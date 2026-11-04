"""Scoped database workspace; all actions use saved connections and permissions."""
import asyncio
import json
import math
import time

from helpers import plugins
from usr.plugins.db_hub.helpers import registry, drivers, memory_backend
from usr.plugins.db_hub.helpers.base import DbHubError, ReadOnlyError, VectorDriver


def enabled_plugins(project, profile):
    from types import SimpleNamespace
    agent = SimpleNamespace(config=SimpleNamespace(profile=profile),
        context=SimpleNamespace(get_data=lambda key: project if key == 'project' else None))
    return plugins.get_enabled_plugins(agent)


def resources(schema):
    result = []
    for kind in ('tables', 'collections', 'indexes', 'labels', 'functions', 'ledgers'):
        values = schema.get(kind, [])
        if isinstance(values, dict): values = list(values)
        for item in values:
            if isinstance(item, dict):
                name = item.get('table_name') or item.get('table') or item.get('name') or item.get('id')
                prefix = item.get('table_schema') or item.get('schema')
                parts = [str(v) for v in (prefix, name) if v]
            else:
                parts = [str(item)]
            if parts: result.append({'name': '.'.join(parts), 'parts': parts, 'kind': kind})
    for dataset, tables in schema.get('datasets', {}).items():
        for name in tables:
            result.append({'name': f'{dataset}.{name}', 'parts': [dataset, str(name)], 'kind': 'tables'})
    return result


def summary(project='', profile=''):
    cfg = registry.load_config(None, project_name=project, agent_profile=profile)
    active = 'db_hub' in enabled_plugins(project, profile)
    backend = cfg.get('memory_backend', {})
    names = [x['connection'] for x in backend.get('connections', [])]
    external = active and backend.get('enabled', False)
    problem = ''
    if external:
        from usr.plugins.db_hub.helpers import compatibility
        check = compatibility.status(project_name=project, agent_profile=profile)
        if not check['compatible']:
            external = False
            problem = check['message']
    return {'connections': [dict(id=c['id'], driver=c['driver'], enabled=c['enabled'],
                description=c['description'], readonly=registry.effective_readonly(c,cfg,None),
                capabilities=sorted(drivers.get_driver_info(c['driver'])['capabilities']),
                memory=external and c['id'] in names) for c in cfg['connections']],
            'memory': {'external': bool(external), 'connections': names if external else [],
                       'blocked_reason': problem,
                       'builtin_enabled': '_memory' in enabled_plugins(project, profile),
                       'prefix': backend.get('prefix', 'mem')}, 'plugin_enabled': active}


def single_statement(statement):
    # Multiple commands are unnecessary in the interactive editor. Reject them
    # conservatively, including semicolons inside strings, before dispatch.
    statement = str(statement or '').strip().rstrip(';').strip()
    if not statement or ';' in statement or len(statement) > 100000:
        raise DbHubError('Run one statement at a time (without embedded semicolons), up to 100 KB.')
    return statement


async def execute(data, project='', profile=''):
    action = data.get('action', 'list')
    if action == 'list': return summary(project,profile)
    if 'db_hub' not in enabled_plugins(project, profile):
        raise DbHubError('Enable Database Hub in this scope to use the workspace.')
    bound = await registry.get_connection(str(data.get('connection') or ''), project_name=project, agent_profile=profile)
    try:
        info = drivers.get_driver_info(bound.conn_cfg['driver'])
        driver = bound.conn_cfg['driver']
        limit = max(1, min(int(data.get('limit',50)), int(bound.glob.get('max_rows',200)), 200))
        started = time.monotonic()
        if action == 'schema':
            result = await bound.schema(str(data.get('target') or ''))
            result = {'schema': result, 'resources': resources(result)}
        elif action == 'preview':
            target = str(data.get('target') or '')
            schema = await bound.schema()
            resource = next((item for item in resources(schema) if item['name'] == target), None)
            if not resource: raise DbHubError('Refresh the schema and select a returned table or collection.')
            offset = max(0, min(int(data.get('offset',0)),100000))
            if 'sql' in info['capabilities']:
                parts = resource['parts']
                if driver == 'bigquery':
                    if any('`' in p for p in parts): raise DbHubError('Unsupported table identifier.')
                    identifier = '`' + '.'.join(parts) + '`'
                else:
                    identifier = '.'.join('"'+p.replace('"','""')+'"' for p in parts)
                if driver == 'spacetimedb' and offset:
                    raise DbHubError('SpacetimeDB does not support OFFSET. Use WHERE and LIMIT in Query.')
                paging = '' if driver == 'spacetimedb' else f' OFFSET {offset}'
                result = await bound.query(f'SELECT * FROM {identifier} LIMIT {limit}' + paging,readonly=True)
                result['next_offset'] = offset + limit if driver != 'spacetimedb' and len(result.get('rows',[])) == limit else None
            elif driver == 'mongodb':
                result = await bound.query(target,{'op':'find','limit':limit,'skip':offset},readonly=True)
                result['next_offset'] = offset + limit if len(result.get('rows',[])) == limit else None
            elif driver in {'neo4j','hydradb'}:
                label = target.replace('`','``')
                result = await bound.query(f'MATCH (n:`{label}`) RETURN n LIMIT {limit}',readonly=True)
            elif hasattr(bound.driver,'browse'):
                result = await asyncio.wait_for(bound.driver.browse(target,limit,data.get('cursor')),bound._timeout())
            else:
                result = {'schema': await bound.schema(target), 'note':'Use the query or vector search editor to retrieve data from this resource.'}
        elif action == 'query':
            params = data.get('params',{})
            if not isinstance(params,dict): raise DbHubError('Parameters must be a JSON object.')
            statement = str(data.get('statement') or '')
            if {'sql','cypher'} & set(info['capabilities']): statement = single_statement(statement)
            write = bound.driver.api_is_write(params,bound.conn_cfg.get('options',{})) if 'api' in info['capabilities'] else not bound.driver.is_read_statement(statement)
            readonly = registry.effective_readonly(bound.conn_cfg,bound.glob,None) or not bool(data.get('allow_writes'))
            if (write and readonly) or (data.get('allow_writes') and not data.get('confirmed')):
                raise ReadOnlyError('Writes require a writable saved connection, Allow changes, and confirmation of this statement.')
            result = await bound.query(statement,params,readonly=readonly)
        elif action == 'search':
            if not isinstance(bound.driver,VectorDriver): raise DbHubError('This connection does not support vector search.')
            vector = data.get('vector')
            if vector is None:
                query = str(data.get('query') or '').strip()
                if not query: raise DbHubError('Enter search text or an embedding vector.')
                from plugins._model_config.helpers.model_config import get_config,build_model_config
                import models
                cfg = build_model_config(get_config(project_name=project,agent_profile=profile).get('embedding_model',{}), models.ModelType.EMBEDDING)
                model = models.get_embedding_model(cfg.provider,cfg.name,model_config=cfg,**cfg.build_kwargs())
                from types import SimpleNamespace
                vector = await memory_backend._embed(SimpleNamespace(get_embedding_model=lambda:model),query)
            if not isinstance(vector,list) or not vector or len(vector)>65536:
                raise DbHubError('Vector must be a non-empty numeric array.')
            vector = [float(x) for x in vector]
            if not all(math.isfinite(x) for x in vector): raise DbHubError('Vector values must be finite.')
            flt = data.get('filter') or None
            if flt is not None and not isinstance(flt,dict): raise DbHubError('Filter must be a JSON object.')
            result = await asyncio.wait_for(bound.driver.vector_search(str(data.get('target') or ''),vector,limit,flt),bound._timeout())
            result = {'rows':result.get('matches',[])}
        else: raise DbHubError('Unknown workspace action.')
        result['elapsed_ms'] = round((time.monotonic()-started)*1000,1)
        # Values are data, rendered as text; mask any configured secrets that
        # could be returned by a query or error before sending them to the UI.
        return json.loads(bound.mask(json.dumps(result,default=str)))
    except Exception as exc:
        raise DbHubError(bound.mask(str(exc))) from exc

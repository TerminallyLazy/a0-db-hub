"""Published SpacetimeDB tables and reducers; bounded reads, module-owned writes."""
import json

from .common import DbHubError, caps, limit, offset, quote


class SpacetimeDB:
    def __init__(self, bound):
        self.bound = bound

    async def catalog(self):
        schema = await self.bound.schema()
        tables = [dict(name=t['name'], parts=[t['name']], label=t['name'],
                       group='public' if t['access'] == 'Public' else 'private', kind='table') for t in schema['tables']]
        reducers = [dict(name='CALL ' + r['name'], parts=[r['name']], label=r['name'],
                         group='Reducers', kind='reducer') for r in schema['reducers']
                    if 'some' not in r.get('lifecycle', {})]
        memories = []
        if self.bound.conn_cfg.get('options', {}).get('memory_database'):
            info = await self.bound.driver.memory_info()
            memories = [dict(name='Memory / ' + c['name'], parts=[c['name']], label=c['name'],
                             group='Agent memory', kind='memory_collection', dimension=c['dimension'])
                        for c in info['collections']]
        return dict(resources=tables + reducers + memories, family='spacetimedb', visual=False,
                    note='Tables are defined in the published module. Reads show a bounded sample; sorting and offset paging are unavailable. Use Query with CALL reducer_name and params.args for writes. Private declarations may be visible even when data is inaccessible.')

    async def metadata(self, resource):
        if resource['kind'] == 'memory_collection':
            return dict(resource=resource['name'], columns=[dict(name=k, type=t) for k, t in
                        [('id', 'string'), ('text', 'string'), ('area', 'string'), ('timestamp', 'string'),
                         ('vector', 'f64[' + str(resource['dimension']) + ']')]],
                        primary_key=['id'], foreign_keys=[], dimension=resource['dimension'],
                        capabilities=caps(filter=False, sort=False,
                        note='Private memory for this authenticated identity. Use Agent Zero memory tools to save or remove memories. Paging scans the collection up to options.memory_max_scan.'))
        schema = await self.bound.schema(resource['parts'][0])
        if resource['kind'] == 'reducer':
            return dict(resource=resource['name'], columns=[], primary_key=[], foreign_keys=[],
                        configuration=schema['reducers'], capabilities=caps(page=None, filter=False, sort=False,
                        note='Invoke this reducer in Query with CALL ' + resource['parts'][0] + ' and a JSON params.args array. Calls require a writable saved connection and confirmation.'))
        table = schema['tables'][0]
        return dict(resource=resource['name'], columns=[dict(name=c['name'], type=json.dumps(c['type'])) for c in table['columns']],
                    primary_key=table['primary_key'], indexes=table['indexes'], constraints=table['constraints'],
                    foreign_keys=[], access=table['access'], capabilities=caps(page=None, filter=False, sort=False,
                    note='Bounded table sample. Use reducers in Query to change records; define schema in the module and publish with the SpacetimeDB CLI.'))

    async def page(self, resource, data, meta):
        if resource['kind'] == 'memory_collection':
            if data.get('filters') or data.get('native_filter') or (data.get('sort') or {}).get('column'):
                raise DbHubError('Memory browsing supports offset paging; use memory_load for vector recall.')
            return {**await self.bound.driver.memory_page(resource['parts'][0], limit(data), offset(data)),
                    'identities': [], 'note': meta['capabilities']['note']}
        if resource['kind'] == 'reducer':
            return dict(rows=[], note=meta['capabilities']['note'])
        if data.get('offset') or data.get('filters') or data.get('native_filter') or (data.get('sort') or {}).get('column'):
            raise DbHubError('SpacetimeDB shows a first-page sample. Use SQL WHERE and LIMIT in Query to select data.')
        result = await self.bound.query('SELECT * FROM ' + quote(resource['parts'][0]) + ' LIMIT ' + str(limit(data)), readonly=True)
        return dict(rows=result['rows'], identities=[], next_offset=None, note=meta['capabilities']['note'])

"""Prepare all memory areas before activating a saved database backend."""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from usr.plugins.db_hub.helpers import config, memory_backend as memory, registry
from usr.plugins.db_hub.helpers.base import DbHubError


async def prepare(settings, project_name='', agent_profile='', agent=None):
    settings = config.normalize_for_save(settings)
    backend = settings['memory_backend']
    if not backend['enabled']:
        return {'ready': False, 'connections': []}
    if agent is None:
        from plugins._model_config.helpers.model_config import get_config, build_model_config
        import models
        model_config = build_model_config(get_config(project_name=project_name, agent_profile=agent_profile).get('embedding_model', {}), models.ModelType.EMBEDDING)
        model = models.get_embedding_model(model_config.provider, model_config.name, model_config=model_config, **model_config.build_kwargs())
        agent = SimpleNamespace(get_embedding_model=lambda: model)
    vector = await memory._embed(agent, 'Agent Zero memory storage setup')
    dimension = len(vector)
    if not dimension:
        raise DbHubError('The embedding model returned an empty vector.')
    prefix = memory.scoped_prefix(None, backend['prefix'], (project_name, agent_profile))
    results = []
    for target in backend['connections']:
        name = target['connection']; bound = None
        try:
            entry = registry.find_entry(settings, name)
            bound = await registry.get_connection(name, project_name=project_name, agent_profile=agent_profile, entry_override=entry, pool=False)
            bound.glob = settings
            resources = []
            for area in memory.AREAS:
                collection = memory.collection_for(prefix, area)
                await memory._call(bound, memory._ensure_collection, bound, collection, dimension, write=True)
                await validate_layout(bound, collection, dimension)
                # A query validates access and the provider's vector contract without
                # writing a synthetic memory into the user's store.
                await memory._call(bound, bound.driver.vector_search, collection, vector, 1, None)
                resources.append(collection)
            results.append({'connection': name, 'resources': resources, 'dimension': dimension})
        except Exception as exc:
            from usr.plugins.db_hub.helpers.secrets_bridge import mask_text
            reason = mask_text(str(exc), project_name=project_name)
            raise DbHubError(f"Memory setup failed for '{name}': {reason}. Settings were not saved. Any empty storage already created is retained; fix access or use a new prefix, then Save again.") from exc
        finally:
            if bound is not None:
                await bound.driver.close()
    return {'ready': True, 'connections': results}


async def validate_layout(bound, collection, dimension):
    kind = bound.conn_cfg['driver']; driver = bound.driver
    if kind == 'postgres':
        opts = driver._options
        async with driver._pool.acquire() as c:
            columns = await c.fetch("SELECT attname AS name, format_type(atttypid,atttypmod) AS type FROM pg_attribute WHERE attrelid=to_regclass($1) AND attnum>0 AND NOT attisdropped", collection)
            primary = await c.fetchval("SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_constraint p CROSS JOIN LATERAL unnest(p.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=p.conrelid AND a.attnum=k.num WHERE p.conrelid=to_regclass($1) AND p.contype='p'", collection)
        types = {r['name']: r['type'] for r in columns}
        identity = opts.get('id_column', 'id'); vector = opts.get('vector_column', 'embedding'); metadata = opts.get('metadata_column', 'metadata')
        if types.get(identity) != 'text' or types.get(vector) != f'vector({dimension})' or types.get(metadata) != 'jsonb' or list(primary or []) != [identity]:
            raise DbHubError(f"Memory table '{collection}' has an incompatible layout or embedding dimension. Choose a new memory prefix; existing data was not modified.")
    elif kind == 'lancedb':
        def validate():
            schema = driver._db.open_table(collection).schema
            required = {'id', 'text', 'vector', 'meta', 'area', 'timestamp', 'memory_id'}
            if not required.issubset(schema.names) or getattr(schema.field('vector').type, 'list_size', None) != dimension:
                raise DbHubError(f"Memory table '{collection}' has an incompatible schema. Choose a new memory prefix.")
        await asyncio.to_thread(validate)


def prepare_sync(settings, project_name='', agent_profile=''):
    if not settings.get('memory_backend', {}).get('enabled'):
        return None
    from usr.plugins.db_hub.helpers import compatibility
    compatibility.require(project_name=project_name, agent_profile=agent_profile)
    # The save hook is synchronous, sometimes inside Flask's async request loop.
    # Finish preparation in an owned loop before the framework persists settings.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='dbhub-memory-setup') as executor:
        return executor.submit(lambda: asyncio.run(prepare(settings, project_name, agent_profile))).result()


async def prepare_for_agent(agent):
    """Initialize inherited project/profile stores on their first monologue."""
    from plugins._model_config.helpers.model_config import get_config
    scope = registry.scope_key(agent)
    settings = memory._memory_cfg(agent)
    embedding = get_config(project_name=scope[0], agent_profile=scope[1]).get('embedding_model', {})
    key = hashlib.sha256(json.dumps([scope, settings, embedding], sort_keys=True, default=str).encode()).hexdigest()
    if agent.get_data('_dbhub_memory_ready') == key:
        return
    await prepare(settings, *scope, agent=agent)
    agent.set_data('_dbhub_memory_ready', key)

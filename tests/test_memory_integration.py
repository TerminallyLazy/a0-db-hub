"""Regression coverage for routing, real local Qdrant, and pgvector wire data."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from helpers import plugins, cache
from usr.plugins.db_hub.helpers import config, registry, memory_backend as memory
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.helpers.drivers.postgres import PostgresDriver


def cfg(driver='qdrant', readonly=False):
    return {'connections': [{'id': 'db', 'driver': driver, 'dsn': 'http://localhost:6333', 'readonly': readonly}],
            'memory_backend': {'enabled': True, 'connections': [{'connection': 'db'}], 'prefix': 'mem'}}


def agent():
    return SimpleNamespace(config=SimpleNamespace(profile=''),
                           context=SimpleNamespace(get_data=lambda key: None),
                           get_embedding_model=lambda: SimpleNamespace(embed_query=lambda text: [1.0, 0.0, 0.0]))


class ValidationTests(unittest.TestCase):
    def test_invalid_memory_targets_rejected(self):
        for driver in ['snowflake', 'neo4j', 'pinecone', 'redis', 'mongodb', 'milvus', 'weaviate']:
            with self.subTest(driver=driver), self.assertRaisesRegex(ValueError, 'not automatic memory'):
                config.normalize_for_save(cfg(driver))
        with self.assertRaisesRegex(ValueError, 'allow writes'):
            config.normalize_for_save(cfg(readonly=True))
        disabled = cfg()
        disabled['connections'][0]['enabled'] = False
        with self.assertRaisesRegex(ValueError, 'disabled'):
            config.normalize_for_save(disabled)

    def test_project_memory_isolation(self):
        with patch.object(plugins, 'get_plugin_config', return_value={'project_memory_isolation': True}):
            self.assertEqual(memory.scoped_prefix(None, 'mem', ('', '')), 'mem')
            self.assertNotEqual(memory.scoped_prefix(None, 'mem', ('alpha', '')), memory.scoped_prefix(None, 'mem', ('beta', '')))

    def test_real_extension_and_tool_precedence(self):
        from helpers import subagents, extension
        from agent import Agent
        with patch.object(plugins, 'get_enabled_plugins', return_value=['_memory', 'db_hub']), patch.object(cache, 'get', return_value=None), patch.object(cache, 'add'):
            self.assertIn('/usr/plugins/db_hub/', subagents.get_paths(None, 'tools', 'memory_save.py')[0])
            for point, name in [('monologue_start', 'MemoryInit'), ('monologue_end', 'MemorizeMemories'), ('monologue_end', 'MemorizeSolutions'), ('message_loop_prompts_after', 'RecallMemories'), ('message_loop_prompts_after', 'RecallWait')]:
                selected = next(c for c in extension._get_extension_classes(point) if c.__name__ == name)
                self.assertIn('/usr/plugins/db_hub/', selected.execute.__code__.co_filename)


class MemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_qdrant_save_recall_delete(self):
        from usr.plugins.db_hub.helpers import bootstrap
        if not bootstrap.is_importable('qdrant_client'):
            self.skipTest('qdrant client not installed')
        from qdrant_client import QdrantClient
        from usr.plugins.db_hub.helpers.drivers.qdrant import QdrantDriver
        driver = QdrantDriver()
        driver._client = QdrantClient(':memory:')
        settings = config.normalize_for_save(cfg())
        bound = registry.BoundConnection(driver, settings['connections'][0], settings, ('', ''))
        with patch.object(plugins, 'get_plugin_config', return_value=settings), patch.object(registry, 'get_connection', AsyncMock(return_value=bound)):
            item_id = await memory.memory_save(agent(), 'synthetic regression record')
            found = await memory.memory_search(agent(), 'synthetic regression record', 0.7)
            self.assertEqual([item['id'] for item in found], [item_id])
            self.assertEqual(found[0]['doc'].page_content, 'synthetic regression record')
            self.assertEqual(found[0]['doc'].metadata['id'], item_id)
            self.assertEqual(await memory.memory_delete(agent(), [item_id]), 1)
            self.assertEqual(await memory.memory_search(agent(), 'synthetic regression record', 0.0), [])
        await driver.close()

    async def test_pgvector_uses_text_casts_and_supports_delete(self):
        calls = []
        class Connection:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def execute(self, sql, *args):
                calls.append((sql, args))
                return 'DELETE 1' if sql.startswith('DELETE') else 'OK'
            async def fetch(self, sql, *args):
                calls.append((sql, args))
                return [{'id': 'logical', 'embedding': 'unused', 'metadata': '{"text":"synthetic"}', '_distance': 0.0}]
        driver = PostgresDriver()
        driver._pool = SimpleNamespace(acquire=Connection)
        await driver.ensure_collection('mem_main', 3)
        await driver.vector_upsert('mem_main', [{'id': 'logical', 'vector': [1, 0, 0], 'payload': {'text': 'synthetic'}}])
        found = await driver.vector_search('mem_main', [1, 0, 0], 10)
        self.assertEqual(found['matches'][0]['text'], 'synthetic')
        self.assertEqual(await driver.vector_delete('mem_main', ['logical']), {'deleted': 1})
        upsert = next(item for item in calls if item[0].startswith('INSERT'))
        search = next(item for item in calls if item[0].startswith('SELECT'))
        self.assertIn('$2::text::vector', upsert[0])
        self.assertEqual(json.loads(upsert[1][1]), [1, 0, 0])
        self.assertIn('$1::text::vector', search[0])
        self.assertIsInstance(search[1][0], str)

    async def test_failed_recall_is_not_empty_success(self):
        with patch.object(plugins, 'get_plugin_config', return_value=cfg()), patch.object(registry, 'get_connection', AsyncMock(side_effect=DbHubError('offline'))):
            with self.assertRaisesRegex(DbHubError, 'offline'):
                await memory.memory_search(agent(), 'synthetic')
            with self.assertRaisesRegex(DbHubError, 'offline'):
                await memory.memory_delete(agent(), ['mem_main_test'])

    async def test_partial_replication_is_reported(self):
        settings = cfg()
        settings['connections'].append({**settings['connections'][0], 'id': 'second'})
        settings['memory_backend']['connections'].append({'connection': 'second'})
        with patch.object(plugins, 'get_plugin_config', return_value=settings), patch.object(memory, '_save_to_target', AsyncMock(side_effect=[None, DbHubError('offline')])):
            with self.assertRaisesRegex(DbHubError, '1/2 stores saved'):
                await memory.memory_save(agent(), 'synthetic')

    async def test_readonly_memory_write_is_blocked(self):
        settings = cfg(readonly=True)
        bound = registry.BoundConnection(SimpleNamespace(), settings['connections'][0], settings, ('', ''))
        operation = AsyncMock()
        with self.assertRaisesRegex(DbHubError, 'allow writes'):
            await memory._call(bound, operation, write=True)
        operation.assert_not_called()

    async def test_database_recall_wait_consumes_matching_scope(self):
        from agent import LoopData
        from usr.plugins.db_hub.extensions.python.message_loop_prompts_after import _50_recall_memories as recall, _91_recall_wait as wait
        a = agent()
        data = {}
        a.get_data = data.get
        a.set_data = lambda key, value: data.__setitem__(key, value)
        loop = LoopData()
        settings = {**cfg(), 'memory_recall_delayed': False}
        with patch.object(plugins, 'get_plugin_config', return_value=settings):
            data[recall.DATA_NAME_RESULT_SCOPE] = recall.get_recall_scope(a)
            data[recall.DATA_NAME_TASK] = asyncio.create_task(asyncio.sleep(0, result={'memories': 'synthetic recall'}))
            await wait.RecallWait(a).execute(loop_data=loop)
            self.assertEqual(loop.extras_persistent['memories'], 'synthetic recall')
            self.assertIsNone(data[recall.DATA_NAME_RESULT_SCOPE])

    async def test_api_save_preserves_backend_when_omitted(self):
        from usr.plugins.db_hub.api.connections import Connections
        settings = config.normalize_for_save(cfg())
        with patch.object(registry, 'load_config', return_value=settings), patch.object(plugins, 'save_plugin_config') as save, patch.object(registry, 'invalidate_all', AsyncMock()):
            response = await Connections.__new__(Connections).process({'action': 'save', 'connections': settings['connections']}, None)
            self.assertTrue(response['ok'])
            self.assertTrue(save.call_args.kwargs['settings']['memory_backend']['enabled'])

    async def test_init_bypasses_faiss_and_restores_it_when_backend_off(self):
        from usr.plugins.db_hub.extensions.python.monologue_start._10_memory_init import MemoryInit
        from plugins._memory.helpers.memory import Memory
        settings = cfg()
        with patch.object(plugins, 'get_plugin_config', return_value=settings), patch.object(plugins, 'get_enabled_plugins', return_value=['_memory', 'db_hub']), patch('usr.plugins.db_hub.helpers.memory_setup.prepare_for_agent', new_callable=AsyncMock) as prepare, patch.object(Memory, 'get', AsyncMock()) as builtin:
            await MemoryInit(agent()).execute()
            builtin.assert_not_called()
            prepare.assert_awaited_once()
            settings['memory_backend']['enabled'] = False
            await MemoryInit(agent()).execute()
            builtin.assert_awaited_once()

    async def test_automatic_memorization_saves_before_replacing(self):
        from usr.plugins.db_hub.extensions.python.monologue_end._50_memorize_fragments import MemorizeMemories
        from usr.plugins.db_hub.extensions.python.monologue_end._51_memorize_solutions import MemorizeSolutions
        for klass, response in [(MemorizeMemories, '["The user prefers concise reports with supporting sources."]'), (MemorizeSolutions, '[{"problem":"Synthetic regression","solution":"Use scoped storage."}]')]:
            for failure in [False, True]:
                with self.subTest(klass=klass.__name__, failure=failure):
                    events = []
                    async def save(*args, **kwargs):
                        events.append('save')
                        if failure: raise DbHubError('offline')
                        return 'mem_main_new'
                    async def delete(*args, **kwargs): events.append('delete')
                    a = agent()
                    a.history = []
                    a.read_prompt = lambda *args, **kwargs: 'synthetic prompt'
                    a.concat_messages = lambda history: 'synthetic history'
                    a.call_utility_model = AsyncMock(return_value=response)
                    a.context.log = SimpleNamespace(log=lambda **kwargs: None)
                    log_item = SimpleNamespace(update=lambda **kwargs: None)
                    with patch.object(plugins, 'get_plugin_config', return_value={'memory_memorize_replace_threshold': .9}), patch.object(memory, 'memory_search', AsyncMock(return_value=[{'id': 'mem_main_old'}])), patch.object(memory, 'memory_save', save), patch.object(memory, 'memory_delete', delete), patch.object(memory, '_failure', return_value='synthetic failure'):
                        await klass(a).memorize(None, log_item)
                    self.assertEqual(events, ['save'] if failure else ['save', 'delete'])

    async def test_connection_pools_do_not_cross_event_loops(self):
        from usr.plugins.db_hub.helpers import drivers
        created = []
        def create(name):
            driver = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
            created.append(driver)
            return driver
        async def connect():
            return await registry.get_connection('db')
        registry._POOLS.clear()
        try:
            with patch.object(registry, 'load_config', return_value=config.normalize_for_save(cfg())), patch.object(drivers, 'create_driver', create):
                first = await connect()
                second = await asyncio.to_thread(lambda: asyncio.run(connect()))
                self.assertIsNot(first.driver, second.driver)
                self.assertEqual(len(created), 2)
                await registry.close_all()
        finally:
            registry._POOLS.clear()

    def test_cosine_threshold_has_same_meaning_for_postgres_and_qdrant(self):
        self.assertAlmostEqual(memory.to_similarity(.2, 'cosine_distance'), memory.to_similarity(.8, 'cosine_similarity'))

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from usr.plugins.db_hub.helpers import memory_setup, memory_backend, registry
from usr.plugins.db_hub.helpers.base import DbHubError


def settings(enabled=True):
    return {'connections': [{'id': 'db', 'driver': 'qdrant', 'dsn': 'http://localhost:6333', 'readonly': False}], 'memory_backend': {'enabled': enabled, 'connections': [{'connection': 'db'}], 'prefix': 'mem'}}


class MemorySetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_prepares_all_areas_without_inserting_memories(self):
        driver = NS(ensure_collection=AsyncMock(), vector_search=AsyncMock(return_value={'matches': []}), close=AsyncMock())
        bound = registry.BoundConnection(driver, settings()['connections'][0], {}, ('', ''))
        a = NS(get_embedding_model=lambda: NS(embed_query=lambda text: [1., 0., 0.]))
        with patch.object(registry, 'get_connection', AsyncMock(return_value=bound)), patch.object(memory_backend, 'scoped_prefix', return_value='mem_project'):
            result = await memory_setup.prepare(settings(), 'project', '', a)
        self.assertTrue(result['ready']); self.assertEqual([c.args for c in driver.ensure_collection.await_args_list], [('mem_project_main', 3), ('mem_project_fragments', 3), ('mem_project_solutions', 3)])
        self.assertEqual(driver.vector_search.await_count, 3); driver.close.assert_awaited_once()

    async def test_disabled_memory_does_not_connect_or_embed(self):
        with patch.object(registry, 'get_connection', AsyncMock()) as connect, patch.object(memory_backend, '_embed', AsyncMock()) as embed:
            self.assertFalse((await memory_setup.prepare(settings(False)))['ready'])
            connect.assert_not_called(); embed.assert_not_called()

    async def test_failure_is_reported_and_connection_closed(self):
        driver = NS(ensure_collection=AsyncMock(side_effect=DbHubError('permission denied')), close=AsyncMock())
        bound = registry.BoundConnection(driver, settings()['connections'][0], {}, ('', ''))
        with patch.object(registry, 'get_connection', AsyncMock(return_value=bound)), patch.object(memory_backend, '_embed', AsyncMock(return_value=[1.])), patch.object(memory_backend, 'scoped_prefix', return_value='mem'):
            with self.assertRaisesRegex(DbHubError, 'Settings were not saved'): await memory_setup.prepare(settings(), agent=NS())
        driver.close.assert_awaited_once()

    async def test_inherited_scope_prepares_once_until_settings_change(self):
        from plugins._model_config.helpers import model_config
        data = {}; a = NS(config=NS(profile=''), context=NS(get_data=lambda key: 'project' if key == 'project' else None), get_data=data.get, set_data=lambda k,v:data.__setitem__(k,v))
        cfg = settings()
        with patch.object(memory_backend, '_memory_cfg', return_value=cfg), patch.object(model_config, 'get_config', return_value={'embedding_model': {}}), patch.object(memory_setup, 'prepare', AsyncMock()) as prepare:
            await memory_setup.prepare_for_agent(a); await memory_setup.prepare_for_agent(a); self.assertEqual(prepare.await_count, 1)
            cfg['memory_backend']['prefix'] = 'new'; await memory_setup.prepare_for_agent(a); self.assertEqual(prepare.await_count, 2)

    def test_save_hook_finishes_setup_before_invalidating_and_returning_config(self):
        from usr.plugins.db_hub import hooks
        with patch.object(memory_setup, 'prepare_sync', side_effect=DbHubError('setup failed')), patch.object(hooks.runtime, 'invalidate_all_pools_sync') as invalidate:
            with self.assertRaisesRegex(DbHubError, 'setup failed'): hooks.save_plugin_config(settings=settings())
            invalidate.assert_not_called()

    def test_framework_does_not_persist_when_setup_fails(self):
        from helpers import plugins
        from usr.plugins.db_hub import hooks
        with patch.object(memory_setup, 'prepare_sync', side_effect=DbHubError('setup failed')), patch.object(plugins, 'determine_plugin_asset_path', return_value='/unused/config.json'), patch.object(plugins, 'call_plugin_hook', side_effect=lambda plugin, name, **kw: hooks.save_plugin_config(**kw)), patch.object(plugins.files, 'write_file') as write:
            with self.assertRaisesRegex(DbHubError, 'setup failed'):
                plugins.save_plugin_config('db_hub', '', '', settings())
            write.assert_not_called()

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
import zipfile
from unittest.mock import patch, AsyncMock

from usr.plugins.db_hub.helpers import bootstrap, compatibility, memory_setup
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.scripts.package import build


class CompatibilityTests(unittest.TestCase):
    def test_old_core_refuses_memory_before_any_setup(self):
        with patch.object(compatibility.plugins, 'get_plugin_root_priority', None, create=True):
            result = compatibility.status()
            self.assertFalse(result['compatible'])
            with patch.object(memory_setup, 'ThreadPoolExecutor') as executor:
                with self.assertRaisesRegex(DbHubError, 'PR #1943'):
                    memory_setup.prepare_sync({'memory_backend': {'enabled': True}})
                executor.assert_not_called()
                memory_setup.prepare_sync({'memory_backend': {'enabled': False}})

    def test_disabled_supporting_memory_plugin_explains_requirement(self):
        with patch.object(compatibility.plugins, 'get_plugin_root_priority', lambda _: 0, create=True), patch.object(compatibility.plugins, 'get_enabled_plugins', return_value=['db_hub']):
            self.assertIn('Keep the built-in Memory plugin enabled', compatibility.status()['message'])

    def test_profile_override_is_not_misreported_as_database_memory(self):
        with patch.object(compatibility.plugins, 'get_plugin_root_priority', lambda _: 0, create=True), patch.object(compatibility.plugins, 'get_enabled_plugins', return_value=['db_hub', '_memory']), patch.object(compatibility.plugins, 'find_plugin_dir', return_value='/a0/usr/plugins/db_hub'), patch.object(compatibility.subagents, 'get_paths', return_value=['/a0/agents/custom/tools/memory_save.py']):
            self.assertIn('overrides memory_save', compatibility.status()['message'])

    def test_unique_guard_fails_before_bundled_memory_on_older_core(self):
        from usr.plugins.db_hub.extensions.python.monologue_start._00_dbhub_memory_guard import DbHubMemoryGuard
        from usr.plugins.db_hub.helpers import memory_backend
        with patch.object(memory_backend, 'backend_enabled', return_value=True), patch.object(compatibility.plugins, 'get_plugin_root_priority', None, create=True):
            with self.assertRaises(DbHubError):
                asyncio.run(DbHubMemoryGuard(NS()).execute())
        with patch.object(memory_backend, 'backend_enabled', return_value=False), patch.object(compatibility, 'require') as require:
            asyncio.run(DbHubMemoryGuard(NS()).execute())
            require.assert_not_called()


class DependencyTests(unittest.TestCase):
    def test_plugin_packages_do_not_precede_framework_paths(self):
        with tempfile.TemporaryDirectory() as path, patch.object(bootstrap, 'deps_dir', return_value=path), patch.object(bootstrap, '_path_ready', False), patch.object(sys, 'path', [path, *sys.path]):
            before = [p for p in sys.path if p != path]
            bootstrap.ensure_deps_path()
            self.assertEqual(sys.path, [*before, path])

    def test_failed_install_keeps_previous_files_and_hides_registry_secrets(self):
        with tempfile.TemporaryDirectory() as path:
            target=Path(path)/'deps';target.mkdir();(target/'keep').write_text('previous')
            result=NS(returncode=1,stderr='https://user:secret@example.test',stdout='')
            with patch.object(bootstrap,'deps_dir',return_value=str(target)), patch.object(bootstrap,'framework_versions',return_value={'numpy':'1.26.4'}), patch.object(bootstrap.subprocess,'run',return_value=result):
                with self.assertRaisesRegex(RuntimeError,'previous installation was retained') as caught:bootstrap.pip_install(bootstrap.BASE_DEPS)
            self.assertNotIn('secret',str(caught.exception));self.assertEqual((target/'keep').read_text(),'previous')

    def test_repair_retains_previously_installed_optional_drivers(self):
        specs=bootstrap._requested_specs([],{'packages':{'chromadb':'old','duckdb':'old','not-a-driver':'1'}})
        self.assertIn('chromadb==1.5.9',specs);self.assertIn('duckdb==1.5.6',specs)
        self.assertFalse(any('not-a-driver' in s for s in specs))


class PackageTests(unittest.TestCase):
    def test_release_contains_every_dispatchable_plugin_tool(self):
        root = Path(__file__).resolve().parents[1]
        expected = {"tools/" + name + ".py" for name in (
            "db_status", "db_schema", "db_query", "db_vector", "db_memory_migrate",
            "memory_save", "memory_load", "memory_delete", "memory_forget",
        )}
        with tempfile.TemporaryDirectory() as path:
            output = Path(path) / "db-hub.zip"
            receipt = build(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertTrue(expected.issubset(archive.namelist()))
                for name in expected:
                    self.assertEqual(archive.read(name), (root / name).read_bytes())
            self.assertTrue(expected.issubset(receipt["files"]))

    def test_release_excludes_owner_state_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path)/'source';root.mkdir()
            for name in ('plugin.yaml','README.md','LICENSE','hooks.py','thumbnail.jpg'):(root/name).write_text('release fixture')
            for name in ('config.json','.toggle-1','.env'):(root/name).write_text('private state')
            (root/'deps').mkdir();(root/'deps/private.py').write_text('private dependency')
            one=build(root,Path(path)/'one.zip');two=build(root,Path(path)/'two.zip')
            self.assertEqual(one['sha256'],two['sha256']);self.assertEqual(len(one['files']),5)

    def test_release_rejects_source_symlinks(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path);(root/'helpers').mkdir();(root/'helpers/leak.py').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError,'symlink'):build(root,root/'out.zip')


class PoolGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_config_cleanup_never_closes_a_new_pool(self):
        from usr.plugins.db_hub.helpers import registry, runtime
        old=registry._Pooled();old.driver=NS(close=AsyncMock())
        new=registry._Pooled();new.driver=NS(close=AsyncMock())
        old_driver=old.driver;new_driver=new.driver
        with patch.object(registry,'_POOLS',{'old':old}):
            runtime.invalidate_all_pools_sync()
            # A request may open a connection before the scheduled close runs.
            registry._POOLS['new']=new
            await asyncio.sleep(0)
            self.assertIs(new.driver,new_driver)
            new_driver.close.assert_not_awaited()
            old_driver.close.assert_awaited_once()

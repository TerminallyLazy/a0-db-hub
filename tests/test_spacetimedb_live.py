"""Opt-in acceptance using real Agent Zero dispatch and a disposable SpacetimeDB.

Publish tests/fixtures/spacetimedb first. Never point this at user data.
Only newly created synthetic records and a temporary Agent Zero project are used.
"""
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid

import httpx

from agent import AgentConfig, AgentContext, LoopData
from helpers import plugins, projects
from helpers.secrets import get_project_secrets_manager
from usr.plugins.db_hub.helpers import registry, workspace
from usr.plugins.db_hub.helpers.explorer import service


@unittest.skipUnless(os.environ.get('DB_HUB_SPACETIME_URL'), 'Set DB_HUB_SPACETIME_URL for the disposable live fixture')
class SpacetimeDBLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_framework_tools_and_workspace_against_real_database(self):
        url = os.environ['DB_HUB_SPACETIME_URL'].rstrip('/')
        database = os.environ.get('DB_HUB_SPACETIME_DATABASE', 'dbhub-spacetime-check')
        project = 'dbhub_spacetime_' + uuid.uuid4().hex
        project_path = Path(projects.get_project_folder(project))
        (project_path / '.a0proj').mkdir(parents=True)
        tokens = []
        async with httpx.AsyncClient(timeout=20) as http:
            for _ in range(2):
                response = await http.post(url + '/v1/identity')
                response.raise_for_status()
                tokens.append(response.json()['token'])
        manager = get_project_secrets_manager(project)
        manager.save_secrets('ST_TEST_A=' + tokens[0] + '\nST_TEST_B=' + tokens[1] + '\n')
        cfg = {'connections': [dict(id=name, driver='spacetimedb', dsn=url, readonly=ro,
                    options={'database': database, 'auth_token': '§§secret(' + key + ')'})
                    for name, ro, key in [('st', False, 'ST_TEST_A'), ('st_ro', True, 'ST_TEST_A'), ('st_other', False, 'ST_TEST_B')]],
               'max_rows': 20, 'readonly_default': True}
        ctx = None
        created = []
        try:
            # Exercise real saved scoped settings, hook normalization and Secrets resolution.
            plugins.save_plugin_config('db_hub', project, '', cfg)
            ctx = AgentContext(config=AgentConfig(mcp_servers='{}'), data={'project': project})
            agent = ctx.agent0
            agent.loop_data = LoopData()
            self.assertIn('db_hub', plugins.get_enabled_plugins(agent))

            async def tool(name, **args):
                selected = agent.get_tool(name, None, args, '', agent.loop_data)
                self.assertIn('/db_hub/', selected.execute.__code__.co_filename)
                await selected.before_execution(**args)
                response = await selected.execute(**args)
                await selected.after_execution(response)
                self.assertFalse(response.break_loop)
                for token in tokens:
                    self.assertNotIn(token, response.message)
                try:
                    return json.loads(response.message)
                except ValueError:
                    return response.message

            status = await tool('db_status', connection='st')
            self.assertTrue(status['results'][0]['ok'])
            schema = await tool('db_schema', connection='st')
            self.assertIn('record', [t['name'] for t in schema['schema']['tables']])
            self.assertIn('private_record', [t['name'] for t in schema['schema']['tables']])
            self.assertIn('add_record', [r['name'] for r in schema['schema']['reducers']])
            private = await tool('db_query', connection='st', statement='SELECT * FROM private_record LIMIT 1')
            self.assertIsInstance(private, str)
            self.assertIn('HTTP', private)
            denied = await tool('db_query', connection='st_ro', statement='CALL add_record', params={'args': ['must not exist']}, readonly=False)
            self.assertIn('read-only', denied)

            marker = 'dbhub-' + uuid.uuid4().hex
            written = await tool('db_query', connection='st', statement='CALL add_record', params={'args': [marker]})
            self.assertTrue(written['ok'])
            found = await tool('db_query', connection='st', statement="SELECT * FROM record WHERE text = '" + marker + "' LIMIT 10")
            self.assertEqual(len(found['rows']), 1)
            identity = found['rows'][0]['id']
            created.append(identity)

            # A different authenticated principal cannot mutate the caller-owned row.
            denied = await tool('db_query', connection='st_other', statement='CALL rename_record', params={'args': [str(identity), 'unauthorized']})
            self.assertIsInstance(denied, str)
            self.assertIn('HTTP', denied)
            unchanged = await tool('db_query', connection='st', statement='SELECT * FROM record WHERE id = ' + str(identity) + ' LIMIT 1')
            self.assertEqual(unchanged['rows'][0]['text'], marker)
            changed = await tool('db_query', connection='st', statement='CALL rename_record', params={'args': [str(identity), marker + '-changed']})
            self.assertTrue(changed['ok'])
            found = await tool('db_query', connection='st', statement='SELECT * FROM record WHERE id = ' + str(identity) + ' LIMIT 1')
            self.assertEqual(found['rows'][0]['text'], marker + '-changed')

            failed = await tool('db_query', connection='st', statement='CALL fail_record', params={'args': [marker + '-rollback']})
            self.assertIsInstance(failed, str)
            rolled_back = await tool('db_query', connection='st', statement="SELECT * FROM record WHERE text = '" + marker + "-rollback' LIMIT 1")
            self.assertEqual(rolled_back['rows'], [])

            catalog = await service.execute({'action': 'catalog', 'connection': 'st'}, project, '', 'test-session')
            self.assertIn('CALL add_record', [r['name'] for r in catalog['resources']])
            page = await service.execute({'action': 'page', 'connection': 'st', 'target': 'record', 'limit': 1}, project, '', 'test-session')
            self.assertLessEqual(len(page['rows']), 1)
            self.assertIsNone(page['next_offset'])
            query = {'action': 'query', 'connection': 'st', 'statement': 'CALL rename_record', 'params': {'args': [str(identity), marker + '-workspace']}, 'allow_writes': True}
            with self.assertRaisesRegex(Exception, 'confirmation'):
                await workspace.execute(query, project, '')
            self.assertTrue((await workspace.execute({**query, 'confirmed': True}, project, ''))['ok'])
            found = await tool('db_query', connection='st', statement='SELECT * FROM record WHERE id = ' + str(identity) + ' LIMIT 1')
            self.assertEqual(found['rows'][0]['text'], marker + '-workspace')
            deleted = await tool('db_query', connection='st', statement='CALL delete_record', params={'args': [str(identity)]})
            self.assertTrue(deleted['ok'])
            self.assertEqual((await tool('db_query', connection='st', statement='SELECT * FROM record WHERE id = ' + str(identity) + ' LIMIT 1'))['rows'], [])
            created.clear()
            # Decimal strings survive JS JSON parsing, then schema-aware encoding
            # restores the exact numeric SATS-JSON required by the HTTP endpoint.
            wide_id = str(9007199254740993 + int(uuid.uuid4().hex[:8], 16))
            result = await tool('db_query', connection='st', statement='CALL add_exact_record', params={'args': [wide_id, marker + '-wide']})
            self.assertTrue(result['ok'])
            created.append(wide_id)
            wide = await tool('db_query', connection='st', statement='SELECT * FROM record WHERE id = ' + wide_id + ' LIMIT 1')
            self.assertEqual(wide['rows'][0]['id'], wide_id)
            self.assertTrue((await tool('db_query', connection='st', statement='CALL delete_record', params={'args': [wide_id]}))['ok'])
            created.clear()
        finally:
            if ctx:
                for identity in created:
                    bound = await registry.get_connection('st', agent=ctx.agent0)
                    await bound.query('CALL delete_record', {'args': [str(identity)]})
                AgentContext.remove(ctx.id)
            await registry.close_all()
            shutil.rmtree(project_path)

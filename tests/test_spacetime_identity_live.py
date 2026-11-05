"""Opt-in real-server acceptance of the same APIs used by UI-only setup.

Set DB_HUB_SPACETIME_IDENTITY_URL to a disposable in-memory server only.
No CLI login, terminal-plugin operation, or user credential is used.
"""
import json
import os
from pathlib import Path
import re
import shutil
import unittest
import uuid
from unittest.mock import patch, AsyncMock

import httpx
from flask import Flask
from agent import AgentConfig, AgentContext, LoopData, UserMessage
from helpers import extension, files, plugins, projects
from helpers.secrets import DEFAULT_SECRETS_FILE, get_project_secrets_manager
from plugins._memory.helpers.memory import Memory
from usr.plugins.db_hub.api.connections import Connections
from usr.plugins.db_hub.helpers import registry, spacetime_identity, spacetime_memory_setup


class Embeddings:
    def embed_query(self, text): return [1., 0., 0.]


@unittest.skipUnless(os.environ.get('DB_HUB_SPACETIME_IDENTITY_URL'), 'Disposable SpacetimeDB identity server not configured')
class IdentityLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_apis_create_identity_store_secrets_and_enable_real_agent_memory(self):
        url = os.environ['DB_HUB_SPACETIME_IDENTITY_URL'].rstrip('/')
        scopes = ['dbhub_identity_' + uuid.uuid4().hex for _ in range(2)]
        paths = [Path(projects.get_project_folder(name)) for name in scopes]
        for path in paths: (path / '.a0proj').mkdir(parents=True)
        global_path = Path(files.get_abs_path(DEFAULT_SECRETS_FILE))
        global_before = global_path.read_bytes() if global_path.exists() else None
        context, receipts = None, []
        app = Flask(__name__)
        app.secret_key = 'disposable-unit-test-session-only'
        try:
            async with httpx.AsyncClient(base_url=url, timeout=90) as http:
                fixture = (await http.post('/v1/identity')).json()
                module, _ = spacetime_memory_setup.module_bundle()
                published = await http.post('/v1/database', params={'host_type': 'Js'}, content=module,
                            headers={'Authorization': 'Bearer ' + fixture['token'], 'Content-Type': 'application/javascript'})
                published.raise_for_status()
                application = published.json()['Success']['database_identity']
                if isinstance(application, dict): application = application['__identity__']
                schema_before = (await http.get('/v1/database/' + application + '/schema', params={'version': '9'})).json()
            api = Connections.__new__(Connections)
            entry = dict(id='st', driver='spacetimedb', dsn=url, readonly=False, options={'database': application})
            replies, credentials = [], []
            for project in scopes:
                manager = get_project_secrets_manager(project)
                manager.save_secrets('IDENTITY_TEST_SENTINEL=preserved\n')
                result = await api.process(dict(action='create_spacetime_identity', connection=entry, project_name=project), None)
                self.assertTrue(result['ok'], result)
                replies.append(result)
                credentials.append(manager.load_secrets()[result['key']])
                self.assertEqual(manager.load_secrets()['IDENTITY_TEST_SENTINEL'], 'preserved')
                receipt = spacetime_identity._receipt_path(url, 'st', project, '')
                receipts.extend([receipt, receipt.with_suffix('.lock')])
                recovered = await api.process(dict(action='create_spacetime_identity', connection=entry, project_name=project), None)
                self.assertEqual(recovered['auth_token'], result['auth_token'])
                self.assertTrue(recovered['recovered'])
            self.assertNotEqual(replies[0]['identity'], replies[1]['identity'])
            self.assertNotIn(replies[0]['key'], get_project_secrets_manager(scopes[1]).load_secrets())
            entry['options']['auth_token'] = replies[0]['auth_token']
            # Session-bound reviewed storage creation uses the real UI API actions.
            with app.test_request_context('/'):
                reviewed = await api.process(dict(action='review_spacetime_memory', connection=entry, project_name=scopes[0]), None)
                self.assertTrue(reviewed['ok'], reviewed)
                created = await api.process(dict(action='create_spacetime_memory', token=reviewed['token'], connection='st', project_name=scopes[0]), None)
            self.assertTrue(created['ready'], created)
            self.assertNotEqual(created['memory_database'], application)
            receipts.append(spacetime_memory_setup._receipt_path(entry, scopes[0], ''))
            entry['options']['memory_database'] = created['memory_database']
            status = await api.process(dict(action='test', connection=entry, project_name=scopes[0]), None)
            self.assertTrue(status['result']['memory_ready'], status)
            settings = dict(connections=[entry], memory_backend={'enabled': True, 'connections': [{'connection': 'st'}], 'prefix': 'mem'})
            with patch('models.get_embedding_model', return_value=Embeddings()):
                saved = await api.process(dict(action='save', project_name=scopes[0], **settings), None)
            self.assertTrue(saved['ok'], saved)
            reopened = await api.process(dict(action='list', project_name=scopes[0]), None)
            self.assertTrue(reopened['memory_backend']['enabled'])
            self.assertEqual(reopened['connections'][0]['options']['auth_token'], replies[0]['auth_token'])
            memory_settings = plugins.get_plugin_config('_memory', project_name=scopes[0])
            memory_settings.update(memory_recall_enabled=True, memory_recall_delayed=False, memory_recall_interval=1,
                                   memory_recall_query_prep=False, memory_recall_post_filter=False)
            plugins.save_plugin_config('_memory', scopes[0], '', memory_settings)
            context = AgentContext(config=AgentConfig(mcp_servers='{}'), data={'project': scopes[0]})
            agent = context.agent0
            agent.loop_data = LoopData()
            agent.get_embedding_model = lambda: Embeddings()
            async def tool(name, **args):
                selected = agent.get_tool(name, None, args, '', agent.loop_data)
                self.assertIn('/db_hub/', selected.execute.__code__.co_filename)
                await selected.before_execution(**args)
                result = await selected.execute(**args)
                await selected.after_execution(result)
                self.assertNotIn('failed', result.message.lower(), result.message)
                for token in credentials: self.assertNotIn(token, result.message)
                return result.message
            with patch.object(Memory, 'get', AsyncMock(side_effect=AssertionError('unexpected built-in storage'))):
                ids = []
                for area in ['main', 'fragments', 'solutions']:
                    result = await tool('memory_save', text='UI identity synthetic ' + area, area=area)
                    ids.append(re.search(r'mem_' + area + r'_[A-Za-z0-9]+', result).group())
                loaded = await tool('memory_load', query='UI identity synthetic', threshold=.95)
                for area in ['main', 'fragments', 'solutions']: self.assertIn('UI identity synthetic ' + area, loaded)
                from helpers.history import History
                agent.history = History(agent)
                agent.loop_data.user_message = agent.hist_add_user_message(UserMessage('UI identity synthetic'))
                for name in ['RecallMemories', 'RecallWait']:
                    klass = next(c for c in extension._get_extension_classes('message_loop_prompts_after', agent) if c.__name__ == name)
                    await klass(agent).execute(loop_data=agent.loop_data)
                self.assertIn('UI identity synthetic main', agent.loop_data.extras_persistent['memories'])
                self.assertIn('UI identity synthetic solutions', agent.loop_data.extras_persistent['solutions'])
                bound = await registry.get_connection('st', agent=agent)
                info = await bound.driver.memory_info()
                self.assertEqual(info['sender'].removeprefix('0x'), replies[0]['identity'])
                other = await registry.get_connection('st', project_name=scopes[1], entry_override={**entry, 'options': {**entry['options'], 'auth_token': replies[1]['auth_token']}}, pool=False)
                try: self.assertEqual((await other.driver.memory_info())['collections'], [])
                finally: await other.driver.close()
                with patch('models.get_embedding_model', return_value=Embeddings()):
                    verified = await api.process(dict(action='verify_memory', project_name=scopes[0]), None)
                self.assertTrue(verified['ok'], verified)
                await tool('memory_forget', query='UI identity synthetic', threshold=.95, filter="area == 'fragments'")
                await tool('memory_delete', ids=ids[0] + ',' + ids[2])
                self.assertNotIn('UI identity synthetic main', await tool('memory_load', query='UI identity synthetic', threshold=.95))
            for token in credentials:
                self.assertNotIn(token, json.dumps([*replies, created, status, saved, reopened]))
            async with httpx.AsyncClient(base_url=url) as http:
                self.assertEqual((await http.get('/v1/database/' + application + '/schema', params={'version': '9'})).json(), schema_before)
            self.assertEqual(global_path.read_bytes() if global_path.exists() else None, global_before)
        finally:
            await registry.close_all()
            if context: AgentContext.remove(context.id)
            for receipt in receipts: receipt.unlink(missing_ok=True)
            for path in paths: shutil.rmtree(path, ignore_errors=True)

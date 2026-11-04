"""Opt-in acceptance through actual Agent Zero settings, dispatch and hooks.

Only disposable stores: set DB_HUB_MEMORY_LIVE_URL and DB_HUB_MEMORY_PG_DSN.
The SpacetimeDB application fixture must already be published as
DB_HUB_SPACETIME_DATABASE. Embeddings are deterministic; storage is real.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import unittest
import uuid
from unittest.mock import patch, AsyncMock

import httpx
from agent import AgentConfig, AgentContext, LoopData, UserMessage
from helpers import extension, plugins, projects
from helpers.secrets import get_project_secrets_manager
from plugins._memory.helpers.memory import Memory
from usr.plugins.db_hub.api.connections import Connections
from usr.plugins.db_hub.helpers import registry, memory_backend as memory, spacetime_memory_setup as setup
from usr.plugins.db_hub.helpers.explorer import service


class Embeddings:
    def embed_query(self, text):
        return [0., 1., 0.] if 'orthogonal' in text else [1., 0., 0.]


@unittest.skipUnless(os.environ.get('DB_HUB_MEMORY_LIVE_URL'), 'Disposable memory stores not configured')
class MemoryLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_zero_memory_across_supported_backends(self):
        url = os.environ['DB_HUB_MEMORY_LIVE_URL'].rstrip('/')
        for kind in ('postgres', 'qdrant', 'lancedb', 'chromadb', 'spacetimedb'):
            with self.subTest(backend=kind):
                project = 'dbhub_memory_' + uuid.uuid4().hex
                other = project + '_other'
                path = Path(projects.get_project_folder(project))
                other_path = Path(projects.get_project_folder(other))
                (path / '.a0proj').mkdir(parents=True)
                (other_path / '.a0proj').mkdir(parents=True)
                context = None
                receipt = None
                try:
                    entry = dict(id='memory', driver=kind, readonly=False, options={})
                    if kind == 'postgres':
                        get_project_secrets_manager(project).save_secrets('MEMORY_TEST_DSN=' + os.environ['DB_HUB_MEMORY_PG_DSN'] + '\n')
                        entry['dsn'] = '§§secret(MEMORY_TEST_DSN)'
                    elif kind == 'spacetimedb':
                        async with httpx.AsyncClient() as http:
                            token = (await http.post(url + '/v1/identity')).json()['token']
                        get_project_secrets_manager(project).save_secrets('MEMORY_TEST_TOKEN=' + token + '\n')
                        entry.update(dsn=url, options={'database': os.environ['DB_HUB_SPACETIME_DATABASE'], 'auth_token': '§§secret(MEMORY_TEST_TOKEN)'})
                        reviewed = await setup.review_setup('synthetic-session', entry, project)
                        created = await setup.create_storage('synthetic-session', reviewed['token'], 'memory', project)
                        self.assertTrue(created['ready'])
                        recovered = await setup.review_setup('synthetic-session', entry, project)
                        self.assertEqual(recovered['recovery'], created['memory_database'])
                        receipt = setup._receipt_path(entry, project, '')
                        entry['options']['memory_database'] = created['memory_database']
                    else:
                        entry['dsn'] = str(path / 'store')
                    settings = dict(connections=[entry], memory_backend={'enabled': True, 'connections': [{'connection': 'memory'}], 'prefix': 'mem'})
                    api = Connections.__new__(Connections)
                    with patch('models.get_embedding_model', return_value=Embeddings()):
                        saved = await api.process(dict(action='save', project_name=project, **settings), None)
                    self.assertTrue(saved['ok'], saved)
                    # Reopen from disk through the settings API; no mocked settings/config.
                    reopened = await api.process(dict(action='list', project_name=project), None)
                    self.assertTrue(reopened['memory_backend']['enabled'])
                    self.assertEqual(reopened['memory_backend']['connections'][0]['connection'], 'memory')
                    recall_settings = plugins.get_plugin_config('_memory', project_name=project)
                    recall_settings.update({
                        'memory_recall_enabled': True, 'memory_recall_delayed': False,
                        'memory_recall_interval': 1, 'memory_recall_query_prep': False,
                        'memory_recall_post_filter': False, 'project_memory_isolation': True,
                    })
                    plugins.save_plugin_config('_memory', project, '', recall_settings)
                    context = AgentContext(config=AgentConfig(mcp_servers='{}'), data={'project': project})
                    agent = context.agent0
                    agent.loop_data = LoopData()
                    agent.get_embedding_model = lambda: Embeddings()

                    async def tool(name, **args):
                        selected = agent.get_tool(name, None, args, '', agent.loop_data)
                        self.assertIn('/db_hub/', selected.execute.__code__.co_filename)
                        await selected.before_execution(**args)
                        response = await selected.execute(**args)
                        await selected.after_execution(response)
                        self.assertFalse(response.break_loop)
                        self.assertNotIn('failed', response.message.lower(), response.message)
                        return response.message

                    with patch.object(Memory, 'get', AsyncMock(side_effect=AssertionError('unexpected built-in memory access'))):
                        init = next(c for c in extension._get_extension_classes('monologue_start', agent) if c.__name__ == 'MemoryInit')
                        await init(agent).execute()
                        ids = []
                        for area in memory.AREAS:
                            response = await tool('memory_save', text='synthetic ' + area + ' memory', area=area)
                            ids.append(re.search(r'mem_' + area + r'_[A-Za-z0-9]+', response).group())
                        await tool('memory_save', text='orthogonal excluded memory', area='main')
                        loaded = await tool('memory_load', query='synthetic recall', threshold=.95)
                        for area in memory.AREAS:
                            self.assertIn('synthetic ' + area + ' memory', loaded)
                        self.assertNotIn('orthogonal excluded', loaded)
                        selected = await tool('memory_load', query='synthetic recall', threshold=.95, filter="area == 'solutions'")
                        self.assertIn('synthetic solutions memory', selected)
                        self.assertNotIn('synthetic main memory', selected)
                        # Real automatic recall classes and result application to prompts.
                        recall = next(c for c in extension._get_extension_classes('message_loop_prompts_after', agent) if c.__name__ == 'RecallMemories')
                        wait = next(c for c in extension._get_extension_classes('message_loop_prompts_after', agent) if c.__name__ == 'RecallWait')
                        self.assertIn('/db_hub/', recall.execute.__code__.co_filename)
                        # A new user turn supplies the query; prior tool logs are
                        # deliberately excluded from this deterministic embedding.
                        from helpers.history import History
                        agent.history = History(agent)
                        agent.loop_data.user_message = agent.hist_add_user_message(UserMessage('synthetic recall'))
                        await recall(agent).execute(loop_data=agent.loop_data)
                        await wait(agent).execute(loop_data=agent.loop_data)
                        self.assertIn('memories', agent.loop_data.extras_persistent)
                        self.assertIn('synthetic main memory', agent.loop_data.extras_persistent['memories'])
                        self.assertIn('synthetic solutions memory', agent.loop_data.extras_persistent['solutions'])
                        # Same connection/storage, separate Agent Zero project namespace.
                        plugins.save_plugin_config('_memory', other, '', {'project_memory_isolation': True})
                        bound = await registry.get_connection('memory', agent=agent)
                        other_prefix = memory.scoped_prefix(None, 'mem', (other, ''))
                        for area in memory.AREAS:
                            await bound.driver.ensure_collection(memory.collection_for(other_prefix, area), 3)
                        own_prefix = memory.scoped_prefix(agent, 'mem')
                        self.assertNotEqual(own_prefix, other_prefix)
                        self.assertEqual((await bound.driver.vector_search(other_prefix + '_main', [1., 0., 0.], 10))['matches'], [])
                        if kind == 'spacetimedb':
                            catalog = await service.execute(dict(action='catalog', connection='memory'), project, '', 'test')
                            self.assertIn('Memory / ' + own_prefix + '_main', [r['name'] for r in catalog['resources']])
                            page = await service.execute(dict(action='page', connection='memory', target='Memory / ' + own_prefix + '_main', limit=1), project, '', 'test')
                            self.assertEqual(len(page['rows']), 1)
                            self.assertEqual(page['next_offset'], 1)
                        await tool('memory_delete', ids=ids[0])
                        self.assertNotIn('synthetic main memory', await tool('memory_load', query='synthetic', threshold=.95))
                        await tool('memory_forget', query='synthetic', threshold=.95, filter="area == 'fragments'")
                        found = await memory.memory_search(agent, 'synthetic', .95)
                        self.assertEqual([row['id'] for row in found], [ids[2]])
                        # Driver pool close/reopen proves persistence, not just cache retention.
                        await registry.close_all()
                        self.assertEqual([row['id'] for row in await memory.memory_search(agent, 'synthetic', .95)], [ids[2]])
                        with patch('models.get_embedding_model', return_value=Embeddings()):
                            verified = await api.process(dict(action='verify_memory', project_name=project), None)
                        self.assertTrue(verified['ok'], verified)
                        # Exercise the framework-selected background learning callbacks
                        # with deterministic utility output and real database storage.
                        for klass_name, output, expected, area in [
                            ('MemorizeMemories', '["The user prefers concise reports with supporting sources."]', 'The user prefers concise reports', 'fragments'),
                            ('MemorizeSolutions', '[{"problem":"Synthetic regression","solution":"Use scoped storage."}]', 'Use scoped storage.', 'solutions')]:
                            klass = next(c for c in extension._get_extension_classes('monologue_end', agent) if c.__name__ == klass_name)
                            self.assertIn('/db_hub/', klass.execute.__code__.co_filename)
                            log_item = agent.context.log.log(type='util', heading='Synthetic learning check')
                            with patch.object(agent, 'call_utility_model', AsyncMock(return_value=output)):
                                await klass(agent).memorize(agent.loop_data, log_item)
                            learned = await memory.memory_search(agent, 'synthetic', .95, areas=(area,))
                            self.assertTrue(any(expected in row['doc'].page_content for row in learned), learned)
                        retained_ids = sorted(row['id'] for row in await memory.memory_search(agent, 'synthetic', .95))
                        settings['memory_backend']['enabled'] = False
                        self.assertTrue((await api.process(dict(action='save', project_name=project, **settings), None))['ok'])
                        self.assertFalse(memory.backend_enabled(agent))
                        with patch.object(Memory, 'get', AsyncMock()) as builtin:
                            await init(agent).execute()
                            builtin.assert_awaited_once()
                        # Disabling redirects routing and preserves database records.
                        found = await memory.memory_search(agent, 'synthetic', .95, connection='memory')
                        self.assertEqual(sorted(row['id'] for row in found), retained_ids)
                        settings['memory_backend']['enabled'] = True
                        with patch('models.get_embedding_model', return_value=Embeddings()):
                            self.assertTrue((await api.process(dict(action='save', project_name=project, **settings), None))['ok'])
                        self.assertTrue(memory.backend_enabled(agent))
                        self.assertEqual(sorted(row['id'] for row in await memory.memory_search(agent, 'synthetic', .95)), retained_ids)
                finally:
                    await registry.close_all()
                    if context:
                        AgentContext.remove(context.id)
                    if receipt:
                        receipt.unlink(missing_ok=True)
                    shutil.rmtree(path, ignore_errors=True)
                    shutil.rmtree(other_path, ignore_errors=True)

    async def test_spacetime_private_protocol_isolation_and_rollback(self):
        from usr.plugins.db_hub.helpers.drivers.spacetimedb import SpacetimeDBDriver
        from usr.plugins.db_hub.helpers.base import DbHubError
        url = os.environ['DB_HUB_MEMORY_LIVE_URL'].rstrip('/')
        async with httpx.AsyncClient(timeout=60) as http:
            principals = [(await http.post(url + '/v1/identity')).json() for _ in range(2)]
            bundle, _ = setup.module_bundle()
            response = await http.post(url + '/v1/database', params={'host_type': 'Js'}, content=bundle,
                                       headers={'Authorization': 'Bearer ' + principals[0]['token'], 'Content-Type': 'application/javascript'})
            response.raise_for_status()
            database = response.json()['Success']['database_identity']
            if isinstance(database, dict): database = database['__identity__']
            private = await http.post(url + '/v1/database/' + database + '/sql', content='SELECT * FROM dbhub_memory_entry',
                                      headers={'Authorization': 'Bearer ' + principals[1]['token']})
            self.assertGreaterEqual(private.status_code, 400)
        drivers = []
        try:
            for principal in principals:
                driver = SpacetimeDBDriver()
                await driver.connect({'database': os.environ['DB_HUB_SPACETIME_DATABASE'], 'memory_database': database, 'auth_token': principal['token']}, url)
                drivers.append(driver)
                await driver.ensure_collection('same_collection', 3)
            first, second = drivers
            await first.vector_upsert('same_collection', [{'id': 'same_id', 'vector': [1, 0, 0], 'payload': {'text': 'owner A'}}])
            self.assertEqual((await second.vector_search('same_collection', [1, 0, 0], 10))['matches'], [])
            await second.vector_upsert('same_collection', [{'id': 'same_id', 'vector': [0, 1, 0], 'payload': {'text': 'owner B'}}])
            self.assertEqual((await first.vector_search('same_collection', [1, 0, 0], 10))['matches'][0]['text'], 'owner A')
            await second.vector_delete('same_collection', ids=['same_id'])
            self.assertEqual(len((await first.memory_page('same_collection'))['rows']), 1)
            with self.assertRaisesRegex(DbHubError, 'dimension'):
                await first.ensure_collection('same_collection', 4)
            with self.assertRaisesRegex(DbHubError, 'dimension'):
                await first.vector_upsert('same_collection', [
                    {'id': 'rolled_back', 'vector': [1, 0, 0]}, {'id': 'bad', 'vector': [1, 0]}])
            self.assertEqual([row['id'] for row in (await first.memory_page('same_collection'))['rows']], ['same_id'])
            await first.vector_upsert('same_collection', [{'id': 'second', 'vector': [1, 0, 0], 'payload': {'text': 'owner A'}}])
            first._options['memory_max_scan'] = 1
            for operation in [lambda: first.vector_search('same_collection', [1, 0, 0], 10),
                              lambda: first.vector_delete('same_collection', flt={'text': 'owner A'}),
                              lambda: first.memory_page('same_collection')]:
                with self.assertRaisesRegex(DbHubError, 'scan limit'):
                    await operation()
            first._options['memory_max_scan'] = 10
            self.assertEqual(len((await first.memory_page('same_collection'))['rows']), 2)
            self.assertEqual(await first.vector_delete('same_collection', flt={'text': 'owner A'}), {'deleted': 2})
        finally:
            for driver in drivers: await driver.close()

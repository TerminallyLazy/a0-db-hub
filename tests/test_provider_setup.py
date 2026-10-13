import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from flask import Flask
from usr.plugins.db_hub.helpers import providers
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.api.provider_setup import ProviderSetup


class ProviderSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        providers._SESSIONS.clear()
        self.addCleanup(providers._SESSIONS.clear)

    async def neon(self):
        result = await providers.connect('browser', 'neon', 'synthetic-management-token', ('', ''))
        return result['handle']

    async def test_neon_project_branch_database_role_import(self):
        calls = []
        async def fake(provider, token, path, params=None):
            calls.append((provider, path, params))
            if path == '/projects': return {'projects': [{'id': 'p1', 'name': 'Project One', 'private': 'redacted'}]}
            if path.endswith('/branches'): return {'branches': [{'id': 'b1', 'name': 'development'}]}
            if path.endswith('/databases'): return {'databases': [{'name': 'app'}]}
            if path.endswith('/roles'): return {'roles': [{'name': 'owner', 'password': 'must-not-return'}]}
            if path.endswith('/connection_uri'): return {'uri': 'postgresql://owner:synthetic-db-password@db.example/app?sslmode=require'}
            raise AssertionError(path)
        with patch.object(providers, '_get', fake), patch.object(providers.extract, 'extract_dsn_secret', return_value={'dsn': 'postgresql://owner:§§secret(SAVED)@db.example/app'}) as save:
            handle = await self.neon()
            branches = await providers.resources('browser', handle, ('', ''), 'p1')
            self.assertEqual(branches['branches'][0]['name'], 'development')
            choices = await providers.resources('browser', handle, ('', ''), 'p1', 'b1')
            self.assertNotIn('must-not-return', json.dumps(choices))
            result = await providers.import_connection('browser', handle, ('', ''), {'project': 'p1', 'branch': 'b1', 'database': 'app', 'role': 'owner', 'connection_id': 'neon_app'})
            self.assertIn('§§secret(SAVED)', result['connection']['dsn'])
            self.assertNotIn('synthetic-management-token', json.dumps(result))
            save.assert_called_once()
            self.assertEqual(calls[-1][2]['branch_id'], 'b1')
            self.assertIsNone(result['connection']['readonly'])

    async def test_browser_scope_expiry_and_disconnect(self):
        with patch.object(providers, '_get', AsyncMock(return_value={'projects': [{'id': 'p1', 'name': 'Project One'}]})):
            handle = await self.neon()
            for owner, scope in [('other', ('', '')), ('browser', ('another-project', ''))]:
                with self.assertRaisesRegex(DbHubError, 'expired or belongs'):
                    providers._session(owner, handle, scope)
            providers.disconnect('other', handle)
            self.assertIn(handle, providers._SESSIONS)
            providers.disconnect('browser', handle)
            self.assertNotIn(handle, providers._SESSIONS)
            handle = await self.neon()
            providers._SESSIONS[handle]['expires'] = time.monotonic()-1
            with self.assertRaises(DbHubError): providers._session('browser', handle, ('', ''))
            self.assertNotIn(handle, providers._SESSIONS)

    async def test_supabase_uses_returned_session_pooler_and_existing_password(self):
        async def fake(provider, token, path, params=None):
            if path == '/projects': return [{'id': 'ref123', 'name': 'Existing project'}]
            return [{'pool_mode': 'session', 'connection_string': 'postgresql://postgres.ref123:[YOUR-PASSWORD]@provider-pool.example:5432/postgres'}]
        with patch.object(providers, '_get', fake), patch.object(providers.extract, 'extract_dsn_secret', return_value={'dsn': 'postgresql://postgres.ref123:§§secret(SAVED)@provider-pool.example:5432/postgres'}) as save:
            result = await providers.connect('browser', 'supabase', 'synthetic-token', ('', ''))
            handle = result['handle']
            resources = await providers.resources('browser', handle, ('', ''), 'ref123')
            data = {'project': 'ref123', 'endpoint': resources['endpoints'][0]['id'], 'connection_id': 'supabase_app'}
            with self.assertRaisesRegex(DbHubError, 'existing database password'):
                await providers.import_connection('browser', handle, ('', ''), data)
            data['database_password'] = 'synthetic:with@reserved'
            await providers.import_connection('browser', handle, ('', ''), data)
            dsn = save.call_args.args[0]
            self.assertIn('synthetic%3Awith%40reserved@provider-pool.example:5432', dsn)
            self.assertIn('sslmode=require', dsn)

    async def test_resources_cannot_escape_selected_account_or_branch(self):
        with patch.object(providers, '_get', AsyncMock(return_value={'projects': [{'id': 'p1'}]})) as get:
            handle = await self.neon()
            for project, branch in [('unknown', ''), ('../../secrets', ''), ('p1', 'unknown')]:
                with self.assertRaises(DbHubError):
                    await providers.resources('browser', handle, ('', ''), project, branch)
            self.assertEqual(get.await_count, 1)

    async def test_unsupported_provider_never_sends_token(self):
        with patch.object(providers, '_get', AsyncMock()) as get:
            with self.assertRaises(DbHubError):
                await providers.connect('browser', 'https://attacker.example', 'synthetic-token', ('', ''))
            get.assert_not_called()

    async def test_provider_errors_do_not_echo_body_or_credential(self):
        import httpx
        transport = httpx.MockTransport(lambda request: httpx.Response(403, json={'error': 'synthetic-secret'}))
        real_client = httpx.AsyncClient
        with patch.object(providers.httpx, 'AsyncClient', side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            with self.assertRaises(DbHubError) as raised:
                await providers._get('neon', 'synthetic-secret', '/projects')
            self.assertNotIn('synthetic-secret', str(raised.exception))
            self.assertIn('403', str(raised.exception))

    async def test_api_keeps_auth_and_csrf_and_binds_browser_session(self):
        self.assertTrue(ProviderSetup.requires_auth())
        self.assertTrue(ProviderSetup.requires_csrf())
        app = Flask(__name__); app.secret_key = 'synthetic-test-session'
        with app.test_request_context(), patch.object(providers, 'connect', AsyncMock(return_value={'handle': 'opaque', 'projects': []})) as connect:
            response = await ProviderSetup.__new__(ProviderSetup).process({'action': 'connect', 'provider': 'neon', 'token': 'synthetic', 'project_name': 'scope'}, None)
            self.assertTrue(response['ok'])
            self.assertEqual(connect.call_args.args[3], ('scope', ''))
            self.assertGreater(len(connect.call_args.args[0]), 20)

    async def test_supabase_transaction_pooler_is_offered_and_cache_is_disabled(self):
        async def fake(provider, token, path, params=None):
            if path == '/projects': return [{'id':'ref','name':'Project'}]
            return [{'pool_mode':'transaction','db_host':'returned.pool.example','db_port':6543,'db_user':'postgres.ref','db_name':'postgres'}]
        with patch.object(providers,'_get',fake),patch.object(providers.extract,'extract_dsn_secret',return_value={'dsn':'postgresql://postgres.ref:§§secret(TEST)@returned.pool.example:6543/postgres'}) as save:
            handle=(await providers.connect('browser','supabase','synthetic',('','')))['handle']
            result=await providers.resources('browser',handle,('',''),'ref')
            self.assertEqual(len(result['endpoints']),1)
            self.assertIn('Transaction pooler',result['endpoints'][0]['name'])
            draft=await providers.import_connection('browser',handle,('',''),{'project':'ref','endpoint':result['endpoints'][0]['id'],'database_password':'synthetic-pass'})
            self.assertEqual(draft['connection']['options']['statement_cache_size'],0)
            self.assertIn(':6543/',save.call_args.args[0])

    async def test_supabase_empty_poolers_return_actionable_console_fallback(self):
        with patch.object(providers,'_get',AsyncMock(side_effect=[[{'id':'ref'}],[]])):
            handle=(await providers.connect('browser','supabase','synthetic',('','')))['handle']
            with self.assertRaisesRegex(DbHubError,'console'):
                await providers.resources('browser',handle,('',''),'ref')

"""Identity tokens are verified, scoped and stored without crossing the UI API."""
import fcntl
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import httpx
from usr.plugins.db_hub.helpers import spacetime_identity as identity
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.api.connections import Connections

Client = httpx.AsyncClient
IDENTITY = 'a' * 64
TOKEN = 'synthetic-private-token-never-return'


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.entry = dict(id='st', driver='spacetimedb', dsn='http://fixture.test', options={})
        self.path = Path(self.temp.name) / 'identity.json'
        self.requests = []
        self.secrets = {'EXISTING': 'preserve'}
        self.manager = NS(_lock=threading.RLock(), load_secrets=lambda: self.secrets)
        self.status, self.data, self.verify = 200, {'identity': IDENTITY, 'token': TOKEN}, 204
        def respond(request):
            self.requests.append(request)
            return httpx.Response(self.verify) if request.method == 'GET' else httpx.Response(self.status, json=self.data)
        def save(key, token, manager):
            self.assertIs(manager, self.manager)
            self.assertNotIn(key, self.secrets)
            self.secrets[key] = token
            return key
        self.patches = [patch.object(identity, '_receipt_path', return_value=self.path),
                        patch.object(identity, 'get_default_secrets_manager', return_value=self.manager),
                        patch.object(identity, 'get_project_secrets_manager', return_value=self.manager),
                        patch.object(identity.registry, 'load_config', return_value={'connections': []}),
                        patch.object(identity.extract, 'store_secret', side_effect=save),
                        patch.object(identity.httpx, 'AsyncClient', side_effect=lambda **kwargs: Client(**kwargs, transport=httpx.MockTransport(respond)))]
        for p in self.patches: p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    async def test_api_creates_and_recovers_without_returning_or_overwriting_tokens(self):
        api = Connections.__new__(Connections)
        result = await api.process(dict(action='create_spacetime_identity', connection=self.entry), None)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['identity'], IDENTITY)
        self.assertTrue(result['auth_token'].startswith('§§secret(DB_HUB_ST_ST_'))
        self.assertEqual(self.secrets[result['key']], TOKEN)
        self.assertEqual(self.secrets['EXISTING'], 'preserve')
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(TOKEN, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.requests[0].method, 'POST')
        self.assertEqual(self.requests[0].url.path, '/v1/identity')
        self.assertNotIn('authorization', self.requests[0].headers)
        self.assertEqual(self.requests[1].headers['Authorization'], 'Bearer ' + TOKEN)
        recovered = await identity.create_identity(self.entry)
        self.assertTrue(recovered['recovered'])
        self.assertEqual(recovered['key'], result['key'])
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    async def test_project_and_profile_bind_recovery_and_project_secret_storage(self):
        with patch.object(identity, '_receipt_path', side_effect=lambda server, name, project, profile: Path(self.temp.name) / (project + profile + '.json')) as receipt, patch.object(identity, 'get_project_secrets_manager', return_value=self.manager) as project_manager, patch.object(identity, 'get_default_secrets_manager') as global_manager:
            first = await identity.create_identity(self.entry, 'project', 'profile')
            second = await identity.create_identity(self.entry, 'other', 'profile')
            self.assertNotEqual(first['key'], second['key'])
            project_manager.assert_any_call('project')
            project_manager.assert_any_call('other')
            global_manager.assert_not_called()
            self.assertEqual(receipt.call_args_list[0].args, ('http://fixture.test', 'st', 'project', 'profile'))

    async def test_existing_draft_or_saved_credentials_are_never_replaced(self):
        for options in [{'auth_token': '§§secret(EXISTING)'}, {'memory_database': 'old-memory'}]:
            with self.subTest(options=options), self.assertRaisesRegex(DbHubError, 'existing token'):
                await identity.create_identity({**self.entry, 'options': options})
        with patch.object(identity.registry, 'load_config', return_value={'connections': [{**self.entry, 'options': {'auth_token': '§§secret(EXISTING)'}}]}):
            with self.assertRaisesRegex(DbHubError, 'saved connection'):
                await identity.create_identity(self.entry)
        self.assertEqual(self.requests, [])

    async def test_invalid_server_origins_never_receive_requests(self):
        for server in ['file:///tmp/secrets', 'http://user:password@fixture.test', 'https://fixture.test/path', 'https://fixture.test?token=private']:
            with self.subTest(server=server), self.assertRaises((DbHubError, ValueError)):
                await identity.create_identity({**self.entry, 'dsn': server})
        self.assertEqual(self.requests, [])

    async def test_provider_error_bodies_and_invalid_tokens_do_not_escape(self):
        for status, data, verify in [(403, {'error': TOKEN}, 204), (200, {'identity': IDENTITY, 'token': TOKEN}, 401),
                                     (200, {'identity': 'invalid', 'token': TOKEN}, 204)]:
            with self.subTest(status=status, verify=verify):
                self.status, self.data, self.verify = status, data, verify
                result = await Connections.__new__(Connections).process(dict(action='create_spacetime_identity', connection=self.entry), None)
                self.assertFalse(result['ok'])
                self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.secrets, {'EXISTING': 'preserve'})

    async def test_missing_stored_token_does_not_silently_create_a_new_identity(self):
        result = await identity.create_identity(self.entry)
        del self.secrets[result['key']]
        with self.assertRaisesRegex(DbHubError, 'missing from Secrets'):
            await identity.create_identity(self.entry)
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    async def test_network_exception_cannot_echo_token_or_headers(self):
        def fail(request): raise httpx.ReadTimeout(TOKEN, request=request)
        with patch.object(identity.httpx, 'AsyncClient', side_effect=lambda **kwargs: Client(**kwargs, transport=httpx.MockTransport(fail))):
            result = await Connections.__new__(Connections).process(dict(action='create_spacetime_identity', connection=self.entry), None)
        self.assertFalse(result['ok'])
        self.assertNotIn(TOKEN, json.dumps(result))

    async def test_changed_stored_token_is_not_replaced_by_recovery(self):
        result = await identity.create_identity(self.entry)
        self.secrets[result['key']] = 'different-private-token'
        self.verify = 401
        with self.assertRaisesRegex(DbHubError, 'verify'):
            await identity.create_identity(self.entry)
        self.assertEqual(self.secrets[result['key']], 'different-private-token')
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    async def test_overlapping_creation_is_rejected_before_network(self):
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DbHubError, 'in progress'):
                await identity.create_identity(self.entry)
        self.assertEqual(self.requests, [])

    async def test_secret_survives_final_receipt_write_failure_and_is_recovered(self):
        record = identity._record
        def write(path, data):
            if data['state'] == 'stored': raise OSError('disk failure')
            record(path, data)
        with patch.object(identity, '_record', side_effect=write):
            result = await Connections.__new__(Connections).process(dict(action='create_spacetime_identity', connection=self.entry), None)
            self.assertFalse(result['ok'])
        recovered = await identity.create_identity(self.entry)
        self.assertTrue(recovered['recovered'])
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_api_requires_authentication_and_csrf(self):
        self.assertTrue(Connections.requires_auth())
        self.assertTrue(Connections.requires_csrf())

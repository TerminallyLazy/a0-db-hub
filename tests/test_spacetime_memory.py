"""Memory wire protocol, setup authority, recovery and package integrity."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from usr.plugins.db_hub.helpers import spacetime_memory_setup as setup
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.helpers.drivers.spacetimedb import SpacetimeDBDriver
from usr.plugins.db_hub.helpers.explorer.service import _REVIEWS


class MemoryProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = SpacetimeDBDriver()
        self.driver._options = {'auth_token': 'synthetic-private-token'}
        self.driver._memory_path = '/v1/database/private-memory'
        self.requests = []
        self.result = {'ok': True, 'protocol': 'a0_db_hub_memory', 'version': 1, 'collections': []}
        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json=json.dumps(self.result))
        self.driver._client = httpx.AsyncClient(base_url='http://fixture.test', transport=httpx.MockTransport(respond))

    async def asyncTearDown(self):
        await self.driver.close()

    async def test_requires_stable_identity_and_prepared_database_before_http(self):
        self.driver._options = {}
        with self.assertRaisesRegex(DbHubError, 'stable Bearer'):
            await self.driver.memory_info()
        self.driver._options['auth_token'] = 'synthetic'
        self.driver._memory_path = ''
        with self.assertRaisesRegex(DbHubError, 'dedicated'):
            await self.driver.memory_info()
        self.assertEqual(self.requests, [])

    async def test_protocol_validation_and_server_error_redaction(self):
        self.result['version'] = 999
        with self.assertRaisesRegex(DbHubError, 'version'):
            await self.driver.memory_info()
        self.result = {'ok': False, 'error': 'synthetic-private-token'}
        with self.assertRaises(DbHubError) as caught:
            await self.driver.memory_info()
        self.assertNotIn('synthetic-private-token', str(caught.exception))
        self.result = {'ok': False, 'error': 'dimension_mismatch'}
        with self.assertRaisesRegex(DbHubError, 'dimension'):
            await self.driver.memory_info()

    async def test_confirmed_rpc_targets_separate_database(self):
        await self.driver.memory_info()
        request = self.requests[0]
        self.assertEqual(request.url.path, '/v1/database/private-memory/call/dbhub_memory_info')
        self.assertEqual(request.url.params['confirmed'], 'true')
        self.assertEqual(json.loads(request.content), [])

    async def test_invalid_vectors_and_unbounded_delete_are_rejected(self):
        for vector in [[], [float('nan')], [float('inf')], ['bad']]:
            with self.subTest(vector=vector), self.assertRaises(DbHubError):
                await self.driver.vector_search('mem_main', vector, 10)
        with self.assertRaisesRegex(DbHubError, 'requires IDs'):
            await self.driver.vector_delete('mem_main')
        with self.assertRaisesRegex(DbHubError, 'not both'):
            await self.driver.vector_delete('mem_main', ids=['id'], flt={'area': 'main'})
        self.assertTrue(all(r.url.path.endswith('_info') for r in self.requests))


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'receipt.json'
        self.entry = {'id': 'st', 'driver': 'spacetimedb', 'dsn': 'http://fixture.test',
                      'readonly': False, 'options': {'database': 'application', 'auth_token': '§§secret(ST_TOKEN)'}}
        self.driver = NS(_options={'auth_token': 'resolved-synthetic'}, test=AsyncMock(return_value={'ok': True}),
                         _client=NS(base_url='http://fixture.test'), close=AsyncMock(), memory_info=AsyncMock(),
                         _request=AsyncMock(return_value={'Success': {'database_identity': 'a' * 64}}), _json=lambda value: value)
        self.bound = NS(conn_cfg=self.entry, driver=self.driver, test=self.driver.test, mask=lambda value: value)
        self.patches = [patch.object(setup, '_connection', AsyncMock(return_value=self.bound)),
                        patch.object(setup, '_receipt_path', return_value=self.path)]
        for p in self.patches: p.start()
        _REVIEWS.clear()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()
        _REVIEWS.clear()

    async def test_bundle_manifest_covers_shipped_source(self):
        bundle, digest = setup.module_bundle()
        self.assertEqual(hashlib.sha256(bundle).hexdigest(), digest)
        self.assertNotIn(b'/Users/lazy/', bundle)

    async def test_review_is_bound_to_scope_session_connection_and_single_use(self):
        review = await setup.review_setup('owner', self.entry, 'project', 'profile')
        for owner, project, profile, connection in [('other', 'project', 'profile', 'st'),
                                                  ('owner', 'other', 'profile', 'st'),
                                                  ('owner', 'project', '', 'st'),
                                                  ('owner', 'project', 'profile', 'other')]:
            with self.subTest(owner=owner, project=project, profile=profile, connection=connection), self.assertRaisesRegex(DbHubError, 'another session'):
                await setup.create_storage(owner, review['token'], connection, project, profile)
        self.driver._request.assert_not_called()
        result = await setup.create_storage('owner', review['token'], 'st', 'project', 'profile')
        self.assertEqual(result['memory_database'], 'a' * 64)
        call = self.driver._request.call_args
        self.assertEqual(call.args, ('POST',))
        self.assertEqual(call.kwargs['database_path'], '/v1/database')
        self.assertEqual(call.kwargs['params'], {'host_type': 'Js'})
        self.assertNotIn('resolved-synthetic', self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(DbHubError, 'expired'):
            await setup.create_storage('owner', review['token'], 'st', 'project', 'profile')
        recovered = await setup.review_setup('owner', self.entry, 'project', 'profile')
        self.assertEqual(recovered['recovery'], 'a' * 64)
        self.assertEqual(self.driver._request.await_count, 1)

    async def test_changed_credential_or_module_requires_review_again(self):
        for changed in ['credential', 'module']:
            with self.subTest(changed=changed):
                self.driver._options['auth_token'] = 'resolved-synthetic'
                review = await setup.review_setup('owner', self.entry)
                if changed == 'credential': self.driver._options['auth_token'] = 'changed'
                with patch.object(setup, 'module_bundle', return_value=(b'changed', 'changed')) if changed == 'module' else patch.object(setup, 'module_bundle', wraps=setup.module_bundle):
                    with self.assertRaisesRegex(DbHubError, 'changed'):
                        await setup.create_storage('owner', review['token'], 'st')
        self.driver._request.assert_not_called()

    async def test_lost_response_is_held_and_completed_receipt_recovers(self):
        review = await setup.review_setup('owner', self.entry)
        self.driver._request.side_effect = DbHubError('write outcome unknown')
        with self.assertRaises(DbHubError): await setup.create_storage('owner', review['token'], 'st')
        with self.assertRaisesRegex(DbHubError, 'outcome is unknown'):
            await setup.review_setup('owner', self.entry)
        self.assertEqual(self.driver._request.await_count, 1)

    async def test_two_reviews_cannot_create_duplicate_storage(self):
        first = await setup.review_setup('owner', self.entry)
        second = await setup.review_setup('owner', self.entry)
        await setup.create_storage('owner', first['token'], 'st')
        recovered = await setup.create_storage('owner', second['token'], 'st')
        self.assertEqual(recovered['memory_database'], 'a' * 64)
        self.assertEqual(self.driver._request.await_count, 1)

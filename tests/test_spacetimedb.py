"""SpacetimeDB HTTP contracts and opt-in real-server Agent Zero acceptance.

Unit tests use MockTransport, never credentials or a provider account.
Live checks require DB_HUB_SPACETIME_URL and a disposable published fixture.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from usr.plugins.db_hub.helpers import config, drivers, registry, workspace
from usr.plugins.db_hub.helpers.base import DbHubError, ReadOnlyError
from usr.plugins.db_hub.helpers.drivers.spacetimedb import SpacetimeDBDriver, encode_arg
from usr.plugins.db_hub.helpers.explorer import service, table_setup


SCHEMA = {
    'typespace': {'types': [{'Product': {'elements': [
        {'name': {'some': 'id'}, 'algebraic_type': {'U64': []}},
        {'name': {'some': 'text'}, 'algebraic_type': {'String': []}},
    ]}}]},
    'tables': [{'name': 'record', 'product_type_ref': 0, 'primary_key': [0],
                'table_access': {'Public': []}, 'indexes': [], 'constraints': []},
               {'name': 'private_record', 'product_type_ref': 0, 'primary_key': [0],
                'table_access': {'Private': []}}],
    'reducers': [{'name': 'add_record', 'params': {'elements': [{'name': {'some': 'text'}, 'algebraic_type': {'String': []}}]}, 'lifecycle': {'none': []}},
                 {'name': 'init', 'lifecycle': {'some': {'Init': []}}}],
}


def settings(readonly=True):
    return {'connections': [{'id': 'st', 'driver': 'spacetimedb',
             'dsn': 'http://spacetime.test', 'readonly': readonly,
             'options': {'database': 'fixture'}}], 'max_rows': 2, 'readonly_default': True}


class SpacetimeDBTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = SpacetimeDBDriver()
        self.requests = []
        self.status = 200

        def respond(request):
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, text='sensitive-provider-token', request=request)
            if request.url.path.endswith('/schema'):
                return httpx.Response(200, json=SCHEMA)
            if request.url.path.endswith('/sql'):
                return httpx.Response(200, json=[{'schema': SCHEMA['typespace']['types'][0]['Product'],
                                                  'rows': [[18446744073709551615, 'hello']]}])
            if '/call/' in request.url.path:
                return httpx.Response(200, content=b'')
            return httpx.Response(200, json={'database_identity': 'synthetic'})

        self.driver._client = httpx.AsyncClient(base_url='http://spacetime.test', transport=httpx.MockTransport(respond))
        self.driver._path = '/v1/database/fixture'
        self.bound = registry.BoundConnection(self.driver, settings()['connections'][0], settings(), ('', ''))

    async def asyncTearDown(self):
        await self.driver.close()

    async def test_real_database_health_and_raw_schema(self):
        self.assertTrue((await self.driver.test())['ok'])
        self.assertEqual([r.url.path for r in self.requests], ['/v1/database/fixture', '/v1/database/fixture/schema'])
        self.assertEqual(self.requests[-1].url.params['version'], '9')
        schema = await self.driver.list_schema('record')
        self.assertEqual(schema['tables'][0]['primary_key'], ['id'])
        self.assertEqual((await self.driver.list_schema('private_record'))['tables'][0]['access'], 'Private')
        self.assertEqual((await self.driver.list_schema('add_record'))['reducers'][0]['name'], 'add_record')
        with self.assertRaises(DbHubError):
            await self.driver.list_schema('absent')

    async def test_sql_row_normalization_preserves_wide_integer(self):
        result = await self.bound.query('SELECT * FROM record LIMIT 1')
        self.assertEqual(result['rows'], [{'id': '18446744073709551615', 'text': 'hello'}])
        self.assertEqual(self.requests[-1].content, b'SELECT * FROM record LIMIT 1')
        self.assertEqual(self.requests[-1].url.params['confirmed'], 'true')
        with self.assertRaisesRegex(DbHubError, 'no bind parameters'):
            await self.bound.query('SELECT * FROM record WHERE id = :id', {'id': 1})

    async def test_saved_readonly_and_direct_driver_refuse_all_call_forms(self):
        for text in ['CALL add_record', 'call add_record;', 'CALL db.labels()',
                     'SELECT * FROM record; DELETE FROM record', 'DELETE FROM record',
                     'SELECT * FROM record /* comment */']:
            with self.subTest(text=text):
                with self.assertRaises((ReadOnlyError, DbHubError)):
                    await self.bound.query(text, {'args': ['x']}, readonly=False)
        with self.assertRaises(ReadOnlyError):
            await self.driver.query('CALL add_record', {'args': ['x']}, True)
        self.assertEqual(self.requests, [])

    async def test_writable_call_and_stricter_explicit_readonly(self):
        self.bound.conn_cfg['readonly'] = False
        result = await self.bound.query('CALL add_record', {'args': ['hello'], 'confirmed': False})
        self.assertTrue(result['ok'])
        request = self.requests[-1]
        self.assertEqual(request.url.path, '/v1/database/fixture/call/add_record')
        self.assertEqual(json.loads(request.content), ['hello'])
        self.assertEqual(request.url.params['confirmed'], 'false')
        self.assertEqual(result['rows'], [])
        with self.assertRaises(ReadOnlyError):
            await self.bound.query('CALL add_record', {'args': ['x']}, readonly=True)

    async def test_invalid_calls_and_batches_never_reach_server(self):
        for text, params in [('CALL add_record', {'args': {}}), ('CALL add_record', {'confirmed': 'false'}),
                             ('CALL ../escape', {}), ('CALL add_record', {'args': [], 'unexpected': True}),
                             ('SELECT * FROM record; DELETE FROM record', {}), ('', {})]:
            with self.subTest(text=text, params=params), self.assertRaises(DbHubError):
                await self.driver.query(text, params, False)
        self.assertEqual(self.requests, [])

    async def test_provider_errors_are_redacted_and_not_retried(self):
        self.status = 403
        with self.assertRaises(DbHubError) as error:
            await self.driver.query('CALL add_record', {'args': []}, False)
        self.assertIn('HTTP 403', str(error.exception))
        self.assertNotIn('sensitive-provider-token', str(error.exception))
        self.assertEqual(len(self.requests), 1)
        self.status = 404
        with self.assertRaises(DbHubError):
            await self.driver.test()

    async def test_framework_timeout_reports_ambiguous_write_without_retry(self):
        await self.driver.close()
        calls = []
        async def respond(request):
            if request.url.path.endswith('/schema'):
                return httpx.Response(200, json=SCHEMA)
            calls.append(request)
            await asyncio.sleep(1)
            return httpx.Response(200)
        self.driver._client = httpx.AsyncClient(base_url='http://spacetime.test', transport=httpx.MockTransport(respond))
        self.bound.conn_cfg['readonly'] = False
        with patch.object(self.bound, '_timeout', return_value=.01), self.assertRaisesRegex(DbHubError, 'may have committed'):
            await self.bound.query('CALL add_record', {'args': ['hello']})
        self.assertEqual(len(calls), 1)

    async def test_connection_validation_and_bearer_header(self):
        for url in ['ftp://host', 'http://user:pass@host', 'http://host/path', 'http://host?token=x', 'http://host#fragment']:
            with self.subTest(url=url), self.assertRaises(DbHubError):
                await SpacetimeDBDriver().connect({'database': 'db'}, url)
        for database in ['', '../x', 'db?x=1', 2]:
            with self.subTest(database=database), self.assertRaises(DbHubError):
                await SpacetimeDBDriver().connect({'database': database}, 'http://host')
        other = SpacetimeDBDriver()
        await other.connect({'database': 'my-db', 'auth_token': 'synthetic-token'}, 'https://host/')
        try:
            self.assertEqual(other._client.headers['Authorization'], 'Bearer synthetic-token')
            self.assertFalse(other._client.follow_redirects)
        finally:
            await other.close()

    async def test_integer_arguments_are_encoded_using_schema_only(self):
        self.assertEqual(encode_arg('18446744073709551615', {'U64': []}, []), 18446744073709551615)
        self.assertEqual(encode_arg('123', {'String': []}, []), '123')
        self.assertEqual(encode_arg(['123'], {'Array': {'U64': []}}, []), [123])
        typ = {'Product': {'elements': [{'name': {'some': 'id'}, 'algebraic_type': {'U64': []}}]}}
        self.assertEqual(encode_arg({'id': '123'}, {'Ref': 0}, [typ]), {'id': 123})

    async def test_explorer_bounds_and_module_schema_boundary(self):
        adapter = service.adapter(self.bound)
        catalog = await adapter.catalog()
        self.assertEqual(catalog['family'], 'spacetimedb')
        self.assertFalse(catalog['visual'])
        self.assertIn('CALL add_record', [r['name'] for r in catalog['resources']])
        self.assertNotIn('CALL init', [r['name'] for r in catalog['resources']])
        record = next(r for r in catalog['resources'] if r['name'] == 'record')
        meta = await adapter.metadata(record)
        self.assertFalse(meta['capabilities']['update'])
        page = await adapter.page(record, {'limit': 50, 'native_filter': {}, 'sort': {}}, meta)
        self.assertIsNone(page['next_offset'])
        self.assertNotIn(b'OFFSET', self.requests[-1].content)
        with self.assertRaises(DbHubError):
            await adapter.page(record, {'offset': 50}, meta)
        options = await table_setup.options(self.bound)
        self.assertFalse(options['supported'])
        self.assertIn('module', options['note'])

    async def test_workspace_confirms_calls_and_saved_readonly_still_wins(self):
        with patch.object(workspace, 'enabled_plugins', return_value=['db_hub']), patch.object(registry, 'get_connection', return_value=self.bound):
            call = {'action': 'query', 'connection': 'st', 'statement': 'CALL add_record', 'params': {'args': ['x']}}
            for allow, confirm in [(False, False), (True, False), (True, True)]:
                with self.assertRaisesRegex(DbHubError, 'writable saved connection'):
                    await workspace.execute({**call, 'allow_writes': allow, 'confirmed': confirm})
            self.assertEqual(self.requests, [])
            self.bound.conn_cfg['readonly'] = False
            for allow, confirm in [(False, False), (True, False)]:
                with self.assertRaisesRegex(DbHubError, 'confirmation'):
                    await workspace.execute({**call, 'allow_writes': allow, 'confirmed': confirm})
            self.assertTrue((await workspace.execute({**call, 'allow_writes': True, 'confirmed': True}))['ok'])

    async def test_memory_is_not_advertised_and_tokens_require_secrets(self):
        self.assertNotIn('spacetimedb', drivers.MEMORY_DRIVERS)
        cfg = settings()
        cfg['connections'][0]['options']['auth_token'] = 'plain-token'
        with self.assertRaisesRegex(ValueError, 'auth_token'):
            config.normalize_for_save(cfg)

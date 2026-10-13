import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from usr.plugins.db_hub.helpers.explorer import table_setup as tables, service
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers.base import DbHubError


def setUpModule():
    from usr.plugins.db_hub.helpers.bootstrap import ensure_deps_path
    ensure_deps_path()


class TableSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import sqlalchemy as sa
        from sqlalchemy.pool import StaticPool
        from usr.plugins.db_hub.helpers.drivers.sqlalchemy import SqlAlchemyDriver
        self.engine = sa.create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        driver = SqlAlchemyDriver(); driver._engine = self.engine
        self.bound = registry.BoundConnection(driver, {'id': 'test', 'driver': 'sqlalchemy', 'dsn': 'sqlite://', 'readonly': False}, {'query_timeout_s': 10}, ('', ''))
        self.spec = {'schema': 'main', 'name': 'notes', 'columns': [{'name': 'id', 'type': 'auto_id', 'primary': True}, {'name': 'body', 'type': 'text', 'nullable': False}]}
        service._REVIEWS.clear()

    async def asyncTearDown(self):
        self.engine.dispose()

    async def execute(self, data, owner='browser'):
        with patch.object(service.workspace, 'enabled_plugins', return_value=['db_hub']), patch.object(service.registry, 'get_connection', AsyncMock(return_value=self.bound)):
            return await service.execute({'connection': 'test', **data}, owner=owner)

    async def test_review_then_create_empty_table_with_generated_primary_key(self):
        import sqlalchemy as sa
        options = await self.execute({'action': 'table_options'})
        self.assertIn('main', options['schemas'])
        reviewed = await self.execute({'action': 'review_table', 'definition': self.spec})
        with self.engine.connect() as c: self.assertFalse(sa.inspect(c).has_table('notes'))
        result = await self.execute({'action': 'apply', 'token': reviewed['token'], 'definition': {'name': 'wrong'}})
        self.assertEqual(result['resource'], 'main.notes')
        with self.engine.begin() as c:
            c.exec_driver_sql("INSERT INTO notes(body) VALUES ('synthetic')")
            self.assertEqual(c.exec_driver_sql('SELECT id FROM notes').scalar(), 1)
        with self.assertRaises(DbHubError): await self.execute({'action': 'apply', 'token': reviewed['token']})
        duplicate = await self.execute({'action': 'review_table', 'definition': self.spec})
        with self.assertRaises(DbHubError): await self.execute({'action': 'apply', 'token': duplicate['token']})

    async def test_review_bound_to_owner_permissions_and_connection_destination(self):
        for change in ('owner', 'readonly', 'destination'):
            self.bound.conn_cfg.update(readonly=False, dsn='sqlite://')
            reviewed = await self.execute({'action': 'review_table', 'definition': self.spec})
            if change == 'readonly': self.bound.conn_cfg['readonly'] = True
            if change == 'destination': self.bound.conn_cfg['dsn'] = 'sqlite:///other.db'
            with self.assertRaises(DbHubError): await self.execute({'action': 'apply', 'token': reviewed['token']}, owner='different' if change == 'owner' else 'browser')

    async def test_identifiers_types_and_primary_key_validated(self):
        from copy import deepcopy
        invalid = []
        for key, value in [('name', 'notes; DROP TABLE users'), ('schema', 'unknown')]:
            spec = deepcopy(self.spec); spec[key] = value; invalid.append(spec)
        spec = deepcopy(self.spec); spec['columns'][1]['type'] = 'TEXT); DROP TABLE x'; invalid.append(spec)
        spec = deepcopy(self.spec); spec['columns'][1]['name'] = 'id'; invalid.append(spec)
        spec = deepcopy(self.spec); spec['columns'][0].update(type='integer', primary=False); invalid.append(spec)
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(DbHubError): await self.execute({'action': 'review_table', 'definition': spec})

    async def test_duckdb_create_and_warehouse_dialects(self):
        from usr.plugins.db_hub.helpers.drivers.duckdb import DuckDriver
        driver = DuckDriver(); await driver.connect({}, ':memory:')
        bound = registry.BoundConnection(driver, {'id': 'duck', 'driver': 'duckdb', 'readonly': False}, {}, ('', ''))
        spec = {**self.spec, 'columns': [{'name': 'id', 'type': 'integer', 'primary': True}, {'name': 'payload', 'type': 'json', 'nullable': True}]}
        choices = await tables.options(bound); normalized = tables.normalize(spec, choices)
        await bound.query(tables.compile_sql(bound, normalized), readonly=False)
        self.assertEqual((await driver.list_schema())['tables'][0]['table'], 'notes'); await driver.close()
        for kind, expected in [('postgres', 'JSONB'), ('snowflake', 'VARIANT'), ('bigquery', 'NOT ENFORCED')]:
            sql = tables.compile_sql(NS(conn_cfg={'driver': kind}), normalized)
            self.assertIn(expected, sql); self.assertTrue(sql.startswith('CREATE TABLE'))

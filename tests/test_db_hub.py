"""Plugin-local tests for db_hub: config, secrets, readonly, registry, dispatch.

No network, no real credentials, no usr/ runtime state: secrets file reads and
plugin config lookups are monkeypatched; drivers are fakes.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from helpers import plugins as plugins_helper
from helpers import projects as projects_helper
from helpers import secrets as secrets_module
from helpers.errors import RepairableException
from usr.plugins.db_hub.helpers import config as config_helper
from usr.plugins.db_hub.helpers import drivers as driver_registry
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers import secrets_bridge
from usr.plugins.db_hub.helpers.base import (
    DbDriver,
    ReadOnlyError,
    UnknownConnectionError,
    is_write_statement,
    truncate_rows,
)

ENTRY = {
    "id": "analytics_pg",
    "driver": "postgres",
    "dsn": "postgresql://app:§§secret(PG_PASS)@localhost:5432/app",
    "enabled": True,
    "readonly": None,
    "options": {},
}


def _entry(**overrides):
    item = dict(ENTRY)
    item.update(overrides)
    return item


class FakeDriver(DbDriver):
    capabilities = {"sql"}

    def __init__(self):
        self.connected = []
        self.closed = 0
        self.queries = []

    async def connect(self, conn_cfg, resolved_dsn):
        self.connected.append((dict(conn_cfg), resolved_dsn))

    async def test(self):
        return {"ok": True, "latency_ms": 1.0}

    async def list_schema(self, target=""):
        return {"tables": []}

    async def query(self, statement, params, readonly):
        self.queries.append((statement, params, readonly))
        return {"columns": ["ok"], "rows": [{"ok": 1}], "rowcount": 1}

    async def close(self):
        self.closed += 1


def _fake_agent(project="proj-a", profile="analyst"):
    return SimpleNamespace(
        config=SimpleNamespace(profile=profile),
        context=SimpleNamespace(),
        _db_hub_project=project,
    )


def _patch_project(monkeypatch, mapping):
    def _lookup(context):
        return mapping.get(id(context), "")

    monkeypatch.setattr(
        projects_helper, "get_context_project_name",
        lambda context: mapping.get("project", ""))
    return _lookup


class ConfigTests(unittest.TestCase):
    def test_save_fills_defaults(self):
        normalized = config_helper.normalize_for_save({"connections": [_entry()]})
        self.assertEqual(normalized["max_rows"], 200)
        self.assertEqual(normalized["query_timeout_s"], 30)
        self.assertTrue(normalized["readonly_default"])
        self.assertEqual(normalized["connections"][0]["id"], "analytics_pg")

    def test_save_rejects_bad_id(self):
        with self.assertRaises(ValueError):
            config_helper.normalize_for_save(
                {"connections": [_entry(id="Bad Id!")]})

    def test_save_rejects_unknown_driver(self):
        with self.assertRaisesRegex(ValueError, "Unknown driver"):
            config_helper.normalize_for_save(
                {"connections": [_entry(driver="oracle")]})

    def test_save_rejects_duplicate_ids(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            config_helper.normalize_for_save(
                {"connections": [_entry(), _entry()]})

    def test_save_rejects_empty_dsn(self):
        with self.assertRaisesRegex(ValueError, "dsn"):
            config_helper.normalize_for_save(
                {"connections": [_entry(dsn="  ")]}
            )

    def test_memory_backend_multi_shape(self):
        normalized = config_helper.normalize_for_save({
            "connections": [_entry(readonly=False), _entry(id="second", dsn="http://x", readonly=False)],
            "memory_backend": {
                "enabled": True,
                "connections": [{"connection": "analytics_pg"},
                                {"connection": "second"},
                                {"connection": "analytics_pg"}],
                "prefix": "mem"},
        })
        self.assertEqual(
            [t["connection"] for t in
             normalized["memory_backend"]["connections"]],
            ["analytics_pg", "second"])

    def test_memory_backend_rejects_unknown(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            config_helper.normalize_for_save({
                "connections": [_entry()],
                "memory_backend": {"enabled": True,
                                   "connections": [{"connection": "nope"}]},
            })

    def test_strict_refuses_raw_option_secret(self):
        entry = _entry(options={"api_key": "raw-key-123"})
        with self.assertRaisesRegex(ValueError, "options.api_key"):
            config_helper.normalize_for_save({"connections": [entry]})

    def test_strict_allows_option_placeholders(self):
        entry = _entry(options={"api_key": "§§secret(Q_KEY)"})
        normalized = config_helper.normalize_for_save({"connections": [entry]})
        self.assertEqual(normalized["connections"][0]["options"]["api_key"],
                         "§§secret(Q_KEY)")

    def test_store_secret_without_dsn(self):
        import asyncio
        from unittest.mock import patch
        from usr.plugins.db_hub.api.connections import Connections

        handler = Connections.__new__(Connections)
        with patch("usr.plugins.db_hub.helpers.extract.store_secret",
                   return_value="K") as mock_store:
            result = asyncio.run(handler.process(
                {"action": "store_secret", "dsn": "", "key": "K",
                 "password": "pw", "conn_id": "q"}, None))
        self.assertTrue(result["ok"])
        self.assertEqual(result["key"], "K")
        self.assertEqual(result["dsn"], "")
        mock_store.assert_called_once()

    def test_qdrant_auth_error_guidance(self):
        from usr.plugins.db_hub.helpers.drivers.qdrant import QdrantDriver
        from usr.plugins.db_hub.helpers.base import DbHubError
        import asyncio

        class AuthError(Exception):
            def __init__(self):
                super().__init__(
                    "Unexpected Response: 403 (Forbidden)")
                self.status_code = 403

        async def boom():
            raise AuthError()

        async def _run():
            driver = QdrantDriver()
            try:
                await driver._guarded(boom)
            except DbHubError as exc:
                return str(exc)
            return "no-error"

        message = asyncio.run(_run())
        self.assertIn("403", message)
        self.assertIn("api_key", message)

    def test_qdrant_missing_collection_message(self):
        from usr.plugins.db_hub.helpers.drivers.qdrant import QdrantDriver
        from usr.plugins.db_hub.helpers.base import DbHubError
        import asyncio

        class GoneError(Exception):
            def __init__(self):
                super().__init__("not found")
                self.status_code = 404

        async def boom():
            raise GoneError()

        async def _run():
            driver = QdrantDriver()
            try:
                await driver._guarded(boom, "Qdrant collection 'c' does not exist.")
            except DbHubError as exc:
                return str(exc)
            return "no-error"

        message = asyncio.run(_run())
        self.assertIn("does not exist", message)

    def test_memory_backend_legacy_shape_converts(self):
        normalized = config_helper.normalize_for_save({
            "connections": [_entry(readonly=False)],
            "memory_backend": {"enabled": True, "connection": "analytics_pg",
                               "prefix": "mem"},
        })
        self.assertEqual(normalized["memory_backend"]["connections"],
                         [{"connection": "analytics_pg"}])

    def test_read_is_lenient_and_fills_defaults(self):
        normalized = config_helper.normalize_for_read(
            {"connections": [_entry(), {"id": "Bad Id!", "driver": "postgres",
                                        "dsn": "x"}]})
        self.assertEqual(
            [e["id"] for e in normalized["connections"]], ["analytics_pg"])
        self.assertEqual(normalized["max_rows"], 200)

    def test_read_redacts_raw_password_for_ui(self):
        raw = "postgresql://app:s3cr3t@localhost:5432/app"
        normalized = config_helper.normalize_for_read(
            {"connections": [_entry(dsn=raw)]}, hook_context={"caller": "ui"})
        self.assertNotIn("s3cr3t", normalized["connections"][0]["dsn"])
        self.assertIn("***", normalized["connections"][0]["dsn"])

    def test_read_keeps_placeholder_for_ui(self):
        normalized = config_helper.normalize_for_read(
            {"connections": [_entry()]}, hook_context={"caller": "ui"})
        self.assertEqual(normalized["connections"][0]["dsn"], ENTRY["dsn"])

    def test_save_keeps_valid_last_test(self):
        entry = _entry(last_test={"ok": True, "latency_ms": 12.5,
                                  "at": "2026-10-06T00:00:00", "error": ""})
        normalized = config_helper.normalize_for_save({"connections": [entry]})
        self.assertEqual(normalized["connections"][0]["last_test"],
                         {"ok": True, "latency_ms": 12.5,
                          "at": "2026-10-06T00:00:00", "error": ""})

    def test_save_drops_invalid_last_test(self):
        for bad in ({"ok": "yes"}, "tested", None):
            normalized = config_helper.normalize_for_save(
                {"connections": [_entry(last_test=bad)]})
            self.assertIsNone(normalized["connections"][0]["last_test"])

    def test_save_drops_unknown_fields(self):
        entry = _entry(junk="x")
        normalized = config_helper.normalize_for_save({"connections": [entry]})
        self.assertNotIn("junk", normalized["connections"][0])

    def test_description_passthrough_and_cap(self):
        normalized = config_helper.normalize_for_save(
            {"connections": [_entry(description="Orders and revenue questions")]})
        self.assertEqual(normalized["connections"][0]["description"],
                         "Orders and revenue questions")
        long_desc = "x" * 600
        normalized = config_helper.normalize_for_save(
            {"connections": [_entry(description=long_desc)]})
        self.assertEqual(len(normalized["connections"][0]["description"]), 500)
        with self.assertRaises(ValueError):
            config_helper.normalize_for_save(
                {"connections": [_entry(description=123)]})


class RedactionTests(unittest.TestCase):
    def test_placeholder_preview_is_literal(self):
        view = config_helper.redact_connection(_entry())
        self.assertEqual(view["dsn_preview"], ENTRY["dsn"])
        self.assertTrue(view["has_secret"])
        self.assertNotIn("id", [k for k in view if k == "dsn"])

    def test_raw_password_is_masked(self):
        view = config_helper.redact_connection(
            _entry(dsn="postgresql://app:s3cr3t@localhost:5432/app"))
        self.assertNotIn("s3cr3t", view["dsn_preview"])
        self.assertFalse(view["has_secret"])

    def test_redact_keeps_last_test_without_secrets(self):
        view = config_helper.redact_connection(
            _entry(last_test={"ok": True, "latency_ms": 3.0,
                              "at": "2026-10-06", "error": ""}))
        self.assertEqual(view["last_test"]["ok"], True)
        self.assertEqual(view["last_test"]["latency_ms"], 3.0)

    def test_mask_text_replaces_secret_values(self):
        manager = secrets_module.SecretsManager("db-hub-test-only")
        manager._secrets_cache = {"PG_PASS": "s3cr3t-value"}
        with patch.object(secrets_bridge, "manager_for_scope",
                          return_value=manager):
            masked = secrets_bridge.mask_text("connected with s3cr3t-value ok")
        self.assertNotIn("s3cr3t-value", masked)


class SecretsTests(unittest.TestCase):
    def test_missing_placeholder_raises_repairable(self):
        manager = secrets_module.SecretsManager("db-hub-test-missing")
        manager._secrets_cache = {}
        with patch.object(secrets_bridge, "manager_for_scope",
                          return_value=manager):
            with self.assertRaises(RepairableException):
                secrets_bridge.resolve_placeholders("x §§secret(NOPE) y")

    def test_placeholder_resolves(self):
        manager = secrets_module.SecretsManager("db-hub-test-ok")
        manager._secrets_cache = {"PG_PASS": "pw123"}
        with patch.object(secrets_bridge, "manager_for_scope",
                          return_value=manager):
            resolved = secrets_bridge.resolve_placeholders(
                "postgresql://app:§§secret(PG_PASS)@h/db")
        self.assertEqual(resolved, "postgresql://app:pw123@h/db")

    def test_plain_text_passes_through(self):
        self.assertEqual(
            secrets_bridge.resolve_placeholders("no placeholders"), "no placeholders")


class ReadonlyTests(unittest.TestCase):
    def test_agent_cannot_override_saved_readonly_access(self):
        self.assertTrue(registry.effective_readonly(_entry(readonly=True), {'readonly_default': False}, False))
        self.assertTrue(registry.effective_readonly(_entry(readonly=None), {'readonly_default': True}, False))

    def test_precedence_explicit_over_connection_over_global(self):
        conn = _entry(readonly=False)
        glob = {"readonly_default": True}
        self.assertTrue(registry.effective_readonly(conn, glob, True))
        self.assertFalse(registry.effective_readonly(conn, glob, False))
        self.assertFalse(registry.effective_readonly(conn, glob, None))
        self.assertTrue(registry.effective_readonly(
            _entry(readonly=None), glob, None))
        self.assertFalse(registry.effective_readonly(
            _entry(readonly=None), {"readonly_default": False}, None))

    def test_write_classifier(self):
        for read in ("SELECT 1", "  with x as (select 1) select * from x",
                     "EXPLAIN SELECT 1", "MATCH (n) RETURN n",
                     "CALL db.labels()", "SHOW TABLES"):
            self.assertFalse(is_write_statement(read), read)
        for write in ("INSERT INTO t VALUES (1)", "update t set a=1",
                      "DELETE FROM t", "DROP TABLE t", "CREATE TABLE t (a int)",
                      "WITH x AS (SELECT 1) DELETE FROM t",
                      "CALL db.createIndex('x')", "MATCH (n) DETACH DELETE n",
                      "", "VACUUM t"):
            self.assertTrue(is_write_statement(write), repr(write))

    def test_read_prefix_cannot_hide_writes_or_unknown_procedures(self):
        for statement in (
            "SELECT 1; DROP TABLE protected",
            "SELECT 1; DELETE FROM protected",
            "EXPLAIN ANALYZE CREATE TABLE copied AS SELECT 1",
            "EXPLAIN ANALYZE DROP TABLE protected",
            "SELECT * INTO copied FROM protected",
            "WITH x AS (SELECT 1) DELETE/**/FROM protected",
            "CALL custom.perform_task()",
            "CALL db.labels() YIELD label CREATE (n:Copy {name:label})",
            "MATCH (n) CALL custom.perform_task() RETURN n",
        ):
            with self.subTest(statement=statement):
                self.assertTrue(is_write_statement(statement))
        for statement in ("SELECT 1;", "EXPLAIN ANALYZE SELECT 1", "CALL db.labels() YIELD label RETURN label"):
            self.assertFalse(is_write_statement(statement), statement)

    def test_truncate_rows(self):
        rows, truncated = truncate_rows([1, 2, 3], 2)
        self.assertEqual(rows, [1, 2])
        self.assertTrue(truncated)
        rows, truncated = truncate_rows([1], 5)
        self.assertFalse(truncated)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        registry._POOLS.clear()
        self.created: list[FakeDriver] = []

        def _factory(name):
            driver = FakeDriver()
            self.created.append(driver)
            return driver

        self._factory_patch = patch.object(
            driver_registry, "create_driver", side_effect=_factory)
        self._factory_patch.start()

        self.configs: dict[tuple[str, str], dict] = {}

        def _get_config(plugin_name, agent=None, project_name=None,
                        agent_profile=None, caller="api"):
            if agent is not None:
                scope = (getattr(agent, "_db_hub_project", ""),
                         getattr(agent.config, "profile", ""))
            else:
                scope = (project_name or "", agent_profile or "")
            return self.configs.get(scope, {"connections": [], "max_rows": 200,
                                            "query_timeout_s": 5,
                                            "readonly_default": True})

        self._config_patch = patch.object(
            plugins_helper, "get_plugin_config", side_effect=_get_config)
        self._config_patch.start()

        self._project_patch = patch.object(
            projects_helper, "get_context_project_name",
            side_effect=lambda context: "")
        self._project_patch.start()

        self._resolve_patch = patch.object(
            secrets_bridge, "resolve_placeholders",
            side_effect=lambda text, project_name="", context=None: text.replace(
                "§§secret(PG_PASS)", "pw123"))
        self._resolve_patch.start()

    async def asyncTearDown(self):
        self._factory_patch.stop()
        self._config_patch.stop()
        self._project_patch.stop()
        self._resolve_patch.stop()
        await registry.close_all()

    def _agent(self, project="", profile=""):
        agent = _fake_agent(project=project, profile=profile)
        agent._db_hub_project = project
        return agent

    async def test_unknown_connection_lists_valid_ids(self):
        agent = self._agent()
        self.configs[("", "")] = {"connections": [_entry()],
                                  "max_rows": 200, "query_timeout_s": 5,
                                  "readonly_default": True}
        with self.assertRaises(UnknownConnectionError) as ctx:
            await registry.get_connection("nope", agent=agent)
        self.assertIn("analytics_pg", str(ctx.exception))

    async def test_disabled_connection_refused(self):
        agent = self._agent()
        self.configs[("", "")] = {"connections": [_entry(enabled=False)],
                                  "max_rows": 200, "query_timeout_s": 5,
                                  "readonly_default": True}
        from usr.plugins.db_hub.helpers.base import DbHubError

        with self.assertRaises(DbHubError):
            await registry.get_connection("analytics_pg", agent=agent)

    async def test_pool_keyed_by_scope(self):
        self.configs[("p1", "a")] = {"connections": [_entry()],
                                     "max_rows": 200, "query_timeout_s": 5,
                                     "readonly_default": True}
        self.configs[("p2", "a")] = {"connections": [_entry()],
                                     "max_rows": 200, "query_timeout_s": 5,
                                     "readonly_default": True}
        first = await registry.get_connection(
            "analytics_pg", agent=None, project_name="p1", agent_profile="a")
        again = await registry.get_connection(
            "analytics_pg", agent=None, project_name="p1", agent_profile="a")
        other = await registry.get_connection(
            "analytics_pg", agent=None, project_name="p2", agent_profile="a")
        self.assertIs(first.driver, again.driver)
        self.assertIsNot(first.driver, other.driver)
        self.assertEqual(len(self.created), 2)

    async def test_config_change_reconnects(self):
        scope_cfg = {"connections": [_entry()], "max_rows": 200,
                     "query_timeout_s": 5, "readonly_default": True}
        self.configs[("", "")] = scope_cfg
        first = await registry.get_connection("analytics_pg", agent=None)
        self.configs[("", "")] = {"connections": [_entry(
            dsn="postgresql://app:§§secret(PG_PASS)@other:5432/app")],
            "max_rows": 200, "query_timeout_s": 5, "readonly_default": True}
        second = await registry.get_connection("analytics_pg", agent=None)
        self.assertIsNot(first.driver, second.driver)
        self.assertEqual(first.driver.closed, 1)

    async def test_query_blocks_writes_when_readonly(self):
        self.configs[("", "")] = {"connections": [_entry()],
                                   "max_rows": 200, "query_timeout_s": 5,
                                   "readonly_default": True}
        bound = await registry.get_connection("analytics_pg", agent=None)
        with self.assertRaises(ReadOnlyError):
            await bound.query("DROP TABLE t", {}, True)
        self.assertEqual(bound.driver.queries, [])

    async def test_query_truncates_to_max_rows(self):
        self.configs[("", "")] = {"connections": [_entry()],
                                   "max_rows": 1, "query_timeout_s": 5,
                                   "readonly_default": False}

        class WideDriver(FakeDriver):
            async def query(self, statement, params, readonly):
                return {"columns": ["n"],
                        "rows": [{"n": 1}, {"n": 2}], "rowcount": 2}

        with patch.object(driver_registry, "create_driver",
                          return_value=WideDriver()):
            bound = await registry.get_connection("analytics_pg", agent=None)
            result = await bound.query("SELECT 1", {}, False)
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["truncated"])


class StampApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_stamp_persists_last_test(self):
        from usr.plugins.db_hub.api.connections import Connections

        stored = {"connections": [_entry()], "max_rows": 200,
                  "query_timeout_s": 30, "readonly_default": True}
        saved = {}

        def _fake_load(agent=None, project_name="", agent_profile=""):
            return stored

        def _fake_save(plugin_name, project_name="", agent_profile="",
                       settings=None, caller="api"):
            saved["settings"] = settings

        handler = Connections.__new__(Connections)
        with patch.object(registry, "load_config", side_effect=_fake_load):
            with patch.object(plugins_helper, "save_plugin_config",
                              side_effect=_fake_save):
                result = await handler.process(
                    {"action": "stamp", "id": "analytics_pg",
                     "last_test": {"ok": True, "latency_ms": 4.0,
                                   "at": "2026-10-06", "error": ""}},
                    None)
        self.assertTrue(result["ok"])
        self.assertEqual(stored["connections"][0]["last_test"]["ok"], True)
        self.assertEqual(saved["settings"]["connections"][0]["last_test"]["ok"],
                         True)

    async def test_stamp_unknown_id_fails(self):
        from usr.plugins.db_hub.api.connections import Connections

        stored = {"connections": [_entry()]}
        handler = Connections.__new__(Connections)
        with patch.object(registry, "load_config", return_value=stored):
            result = await handler.process(
                {"action": "stamp", "id": "nope",
                 "last_test": {"ok": True}}, None)
        self.assertFalse(result["ok"])
        self.assertIn("nope", result["error"])

    async def test_stamp_invalid_result_fails(self):
        from usr.plugins.db_hub.api.connections import Connections

        handler = Connections.__new__(Connections)
        result = await handler.process(
            {"action": "stamp", "id": "x", "last_test": {"ok": "maybe"}}, None)
        self.assertFalse(result["ok"])

    async def test_stamp_raw_entry_forces_migration(self):
        from usr.plugins.db_hub.api.connections import Connections

        stored = {"connections": [_entry(
            dsn="postgresql://app:s3cr3t@localhost:5432/app")],
            "max_rows": 200, "query_timeout_s": 30, "readonly_default": True}
        handler = Connections.__new__(Connections)
        with patch.object(registry, "load_config", return_value=stored):
            with patch.object(plugins_helper, "save_plugin_config",
                              side_effect=AssertionError("must not save raw")):
                result = await handler.process(
                    {"action": "stamp", "id": "analytics_pg",
                     "last_test": {"ok": True}}, None)
        self.assertFalse(result["ok"])
        self.assertIn("Secrets", result["error"])


class ImporterTests(unittest.TestCase):
    def test_plain_dsn_passthrough(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        result = importer_helper.normalize_pasted(
            "postgresql://app:§§secret(X)@h/db")
        self.assertIn("§§secret(X)", result["dsn"])
        self.assertIsNone(result["stored_key"])

    def test_sqlalchemy_dialect_stripped(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        result = importer_helper.normalize_pasted(
            "postgresql+psycopg2://app:pw@ep-1.neon.tech/db", conn_id="neon")
        self.assertTrue(result["dsn"].startswith("postgresql://"))
        self.assertNotIn("+psycopg2", result["dsn"])
        self.assertIn("sslmode=require", result["dsn"])

    def test_psql_parsed_without_password(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        result = importer_helper.normalize_pasted(
            "psql -h ep-x-pooler.neon.tech -p 6543 -U app -d mydb", conn_id="neon")
        self.assertIn("@ep-x-pooler.neon.tech:6543/mydb", result["dsn"])
        self.assertIn("§§secret(DB_HUB_NEON_PASS)", result["dsn"])
        self.assertIsNone(result["stored_key"])

    def test_psql_pgpw_stored(self):
        import tempfile
        from helpers.secrets import SecretsManager
        from usr.plugins.db_hub.helpers import importer as importer_helper

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/secrets.env"
            open(path, "w").write("OTHER='keep'\n")
            manager = SecretsManager(path)
            result = importer_helper.normalize_pasted(
                "PGPASSWORD=s3cr3t psql -h h.example -U app -d db",
                conn_id="neon", manager=manager)
            self.assertEqual(result["stored_key"], "DB_HUB_NEON_PASS")
            self.assertIn("§§secret(DB_HUB_NEON_PASS)", result["dsn"])
            self.assertNotIn("s3cr3t", result["dsn"])
            content = open(path).read()
            self.assertIn("OTHER='keep'", content)
            self.assertIn("DB_HUB_NEON_PASS='s3cr3t'", content)

    def test_psql_missing_parts_errors(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper
        from usr.plugins.db_hub.helpers.base import DbHubError

        with self.assertRaises(DbHubError):
            importer_helper.normalize_pasted("psql -h onlyhost")

    def test_looks_convertible(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        self.assertEqual(importer_helper.looks_convertible("psql -h h -U u"), "psql")
        self.assertEqual(
            importer_helper.looks_convertible("postgresql+psycopg2://u@h/db"),
            "sqlalchemy")
        self.assertIsNone(
            importer_helper.looks_convertible("postgresql://u:§§secret(K)@h/db"))
        self.assertIsNone(importer_helper.looks_convertible(""))
        self.assertEqual(
            importer_helper.looks_convertible('mongosh "mongodb://u@h/db"'),
            "mongosh")
        self.assertEqual(
            importer_helper.looks_convertible("snowsql -a acct -u user"),
            "snowsql")
        self.assertEqual(
            importer_helper.looks_convertible("A=1\nB=2"), "env")

    def test_mongosh_uri_extracted_and_stored(self):
        import tempfile
        from helpers.secrets import SecretsManager
        from usr.plugins.db_hub.helpers import importer as importer_helper

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/secrets.env"
            open(path, "w").write("")
            manager = SecretsManager(path)
            result = importer_helper.normalize_pasted(
                'mongosh "mongodb+srv://app:s3cr3t@cluster0.x.mongodb.net/appdb"',
                conn_id="mongo", manager=manager)
            self.assertEqual(result["stored_key"], "DB_HUB_MONGO_PASS")
            self.assertIn("§§secret(DB_HUB_MONGO_PASS)", result["dsn"])
            self.assertNotIn("s3cr3t", result["dsn"])

    def test_snowsql_parsed(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        result = importer_helper.normalize_pasted(
            "snowsql -a myacct -u admin -w WH -d DB -s PUBLIC -r SYSADMIN",
            conn_id="snow")
        self.assertTrue(result["dsn"].startswith("snowflake://admin:§§secret("))
        self.assertIn("@myacct/WH/DB/PUBLIC?role=SYSADMIN", result["dsn"])

    def test_env_snippet_qdrant(self):
        import tempfile
        from helpers.secrets import SecretsManager
        from usr.plugins.db_hub.helpers import importer as importer_helper

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/secrets.env"
            open(path, "w").write("")
            manager = SecretsManager(path)
            result = importer_helper.normalize_pasted(
                "QDRANT_URL=https://xyz.qdrant.cloud\nQDRANT_API_KEY=qkey123\n",
                conn_id="vec", driver="qdrant", manager=manager)
            self.assertEqual(result["dsn"], "https://xyz.qdrant.cloud")
            self.assertEqual(result["options"].get("api_key"),
                             "§§secret(QDRANT_API_KEY)")
            self.assertIn("QDRANT_API_KEY", result["stored"])

    def test_convex_slug_expanded(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        result = importer_helper.normalize_pasted(
            "polished-panda-123", conn_id="cx", driver="convex")
        self.assertEqual(result["dsn"], "https://polished-panda-123.convex.cloud")

    def test_normalize_result_shape(self):
        from usr.plugins.db_hub.helpers import importer as importer_helper

        result = importer_helper.normalize_pasted(
            "postgresql://app@h/db", conn_id="x")
        self.assertIn("options", result)
        self.assertIn("stored", result)


class ExtractSecretTests(unittest.TestCase):
    def _manager(self, tmp, content=""):
        from helpers.secrets import SecretsManager

        path = f"{tmp}/secrets.env"
        open(path, "w").write(content)
        return SecretsManager(path), path

    def test_refuses_existing_key(self):
        import tempfile
        from usr.plugins.db_hub.helpers import extract as extract_helper
        from usr.plugins.db_hub.helpers.base import DbHubError

        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._manager(tmp, "DB_HUB_X_PASS='old'\n")
            with self.assertRaisesRegex(DbHubError, "already exists"):
                extract_helper.extract_dsn_secret(
                    "postgresql://u:newpw@h/db", key="DB_HUB_X_PASS",
                    manager=manager)

    def test_refuses_invalid_key(self):
        import tempfile
        from usr.plugins.db_hub.helpers import extract as extract_helper
        from usr.plugins.db_hub.helpers.base import DbHubError

        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._manager(tmp)
            with self.assertRaisesRegex(DbHubError, "invalid"):
                extract_helper.extract_dsn_secret(
                    "postgresql://u:pw@h/db", key="bad key!", manager=manager)

    def test_invalid_key_error_never_echoes_secrets(self):
        import tempfile
        from usr.plugins.db_hub.helpers import extract as extract_helper
        from usr.plugins.db_hub.helpers.base import DbHubError

        leaked = "postgresql://u:leaked-secret-value@h/db"
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = self._manager(tmp)
            with self.assertRaises(DbHubError) as ctx:
                extract_helper.extract_dsn_secret(
                    "postgresql://u:pw@h/db", key=leaked, manager=manager)
            self.assertNotIn("leaked-secret-value", str(ctx.exception))

    def test_suggest_key(self):
        from usr.plugins.db_hub.helpers import extract as extract_helper

        self.assertEqual(extract_helper.suggest_key("neon"), "DB_HUB_NEON_PASS")
        self.assertEqual(extract_helper.suggest_key("my-db!"), "DB_HUB_MY_DB_PASS")


class PasswordSlotTests(unittest.TestCase):
    def test_raw_replaced(self):
        from usr.plugins.db_hub.helpers.extract import apply_password_slot

        out = apply_password_slot("postgresql://app:oldpw@db:5432/app", "K")
        self.assertEqual(out, "postgresql://app:§§secret(K)@db:5432/app")

    def test_placeholder_swapped(self):
        from usr.plugins.db_hub.helpers.extract import apply_password_slot

        out = apply_password_slot(
            "postgresql://app:§§secret(OLD)@db:5432/app?sslmode=require", "K")
        self.assertEqual(
            out, "postgresql://app:§§secret(K)@db:5432/app?sslmode=require")

    def test_bare_userinfo_gets_slot(self):
        from usr.plugins.db_hub.helpers.extract import apply_password_slot

        out = apply_password_slot("postgresql://app@db:5432/app", "K")
        self.assertEqual(out, "postgresql://app:§§secret(K)@db:5432/app")

    def test_no_userinfo_errors(self):
        from usr.plugins.db_hub.helpers.extract import apply_password_slot
        from usr.plugins.db_hub.helpers.base import DbHubError

        with self.assertRaises(DbHubError):
            apply_password_slot("sqlite:////data/app.db", "K")

    def test_store_secret_action_end_to_end(self):
        import asyncio
        import tempfile
        from unittest.mock import patch
        from helpers.secrets import SecretsManager
        from usr.plugins.db_hub.api.connections import Connections
        from usr.plugins.db_hub.helpers import extract as extract_helper

        handler = Connections.__new__(Connections)
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/secrets.env"
            open(path, "w").write("")
            manager = SecretsManager(path)
            with patch.object(extract_helper, "get_default_secrets_manager",
                              return_value=manager):
                result = asyncio.run(handler.process(
                    {"action": "store_secret",
                     "dsn": "postgresql://app@db:5432/app",
                     "key": "DB_HUB_NEON_PASS", "password": "pw",
                     "conn_id": "neon"}, None))
                content = open(path).read()
        self.assertEqual(content, "DB_HUB_NEON_PASS='pw'\n")
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["dsn"], "postgresql://app:§§secret(DB_HUB_NEON_PASS)@db:5432/app")

    def test_store_secret_action_key_only(self):
        import asyncio
        import tempfile
        from unittest.mock import patch
        from helpers.secrets import SecretsManager
        from usr.plugins.db_hub.api.connections import Connections
        from usr.plugins.db_hub.helpers import extract as extract_helper

        handler = Connections.__new__(Connections)
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/secrets.env"
            open(path, "w").write("")
            manager = SecretsManager(path)
            with patch.object(extract_helper, "get_default_secrets_manager",
                              return_value=manager):
                result = asyncio.run(handler.process(
                    {"action": "store_secret", "dsn": "",
                     "key": "DB_HUB_Q_API", "password": "qkey",
                     "conn_id": "qdr"}, None))
                content = open(path).read()
        self.assertIn("DB_HUB_Q_API='qkey'", content)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["key"], "DB_HUB_Q_API")

    def test_store_secret_empty_password_fails(self):
        import asyncio
        from usr.plugins.db_hub.api.connections import Connections

        handler = Connections.__new__(Connections)
        result = asyncio.run(handler.process(
            {"action": "store_secret", "dsn": "postgresql://app@h/db",
             "key": "K", "password": ""}, None))
        self.assertFalse(result["ok"])


class ReadonlyContractTests(unittest.TestCase):
    def test_base_defaults(self):
        from usr.plugins.db_hub.helpers.base import DbDriver

        class _D(DbDriver):
            async def connect(self, c, d): pass
            async def test(self): return {}
            async def list_schema(self, t=""): return {}
            async def query(self, s, p, r): return {}
            async def close(self): pass

        driver = _D()
        self.assertTrue(driver.is_read_statement("SELECT 1"))
        self.assertFalse(driver.is_read_statement("DROP TABLE t"))
        self.assertTrue(driver.api_is_write({"kind": "mutation"}, {}))
        self.assertFalse(driver.api_is_write({}, {}))
        self.assertTrue(driver.api_is_write({"kind": "action"}, {}))

    def test_redis_command_sets(self):
        from usr.plugins.db_hub.helpers.drivers.redis import RedisDriver

        driver = RedisDriver()
        for read in ("GET k", "hgetall k", "EXISTS k", "KEYS *", "TTL k"):
            self.assertTrue(driver.is_read_statement(read), read)
        for write in ("SET k v", "DEL k", "HSET k f v", "FLUSHDB"):
            self.assertFalse(driver.is_read_statement(write), write)
        self.assertFalse(driver.is_read_statement("SUBSCRIBE ch"))

    def test_mongo_op_sets(self):
        from usr.plugins.db_hub.helpers.drivers.mongodb import MongoDriver

        driver = MongoDriver()
        for read in ("find", "aggregate", "count", "distinct"):
            self.assertFalse(driver.api_is_write({"op": read}, {}), read)
        for write in ("insert_one", "update_many", "delete_one",
                      "find_one_and_replace"):
            self.assertTrue(driver.api_is_write({"op": write}, {}), write)


class MemoryBackendTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self, vector=(1.0, 0.0)):
        class _Model:
            def embed_query(self, text):
                return list(vector)

        return SimpleNamespace(
            config=SimpleNamespace(profile=""),
            context=SimpleNamespace(),
            get_embedding_model=lambda: _Model(),
            handle_intervention=self._noop,
            read_prompt=lambda *a, **k: "PROMPT",
        )

    async def _noop(self, progress=""):
        return None

    def _install_fakes(self, driver_name="qdrant", backend=True):
        from usr.plugins.db_hub.helpers.base import VectorDriver
        from usr.plugins.db_hub.helpers import drivers as driver_registry

        class FakeVector(VectorDriver):
            capabilities = {"vector"}

            def __init__(self):
                self.store = {}
                self.ensured = []

            async def connect(self, conn_cfg, dsn):
                pass

            async def test(self):
                return {"ok": True}

            async def list_schema(self, target=""):
                return {}

            async def query(self, statement, params, readonly):
                raise AssertionError("not used")

            async def ensure_collection(self, collection, dim):
                self.ensured.append((collection, dim))
                self.store.setdefault(collection, {})

            async def vector_search(self, collection, vector, top_k, flt=None):
                return {"matches": [
                    {"id": mid, "_score": score, **payload}
                    for mid, (score, payload) in
                    self.store.get(collection, {}).items()][:top_k]}

            async def vector_upsert(self, collection, items):
                for item in items:
                    self.store.setdefault(collection, {})[item["id"]] = (
                        1.0, dict(item.get("payload", {}) or {}))
                return {"upserted": len(items)}

            async def vector_delete(self, collection, ids=None, flt=None):
                store = self.store.get(collection, {})
                count = 0
                for item_id in ids or []:
                    if store.pop(item_id, None) is not None:
                        count += 1
                return {"deleted": count}

            async def close(self):
                pass

        fake = FakeVector()
        entry = _entry(driver=driver_name, readonly=False,
                       dsn="scheme://h/db" if driver_name != "qdrant"
                       else "http://localhost:6333")
        cfg = {"connections": [entry], "max_rows": 200, "query_timeout_s": 5,
               "readonly_default": True,
               "memory_backend": {"enabled": backend, "connection": "analytics_pg",
                                  "prefix": "mem"}}

        import unittest.mock as mock

        patches = [
            mock.patch.object(plugins_helper, "get_plugin_config",
                              return_value=cfg),
            mock.patch.object(driver_registry, "create_driver",
                              return_value=fake),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        registry._POOLS.clear()
        self.addCleanup(registry._POOLS.clear)
        return fake

    async def test_backend_off_routes_nothing(self):
        from usr.plugins.db_hub.helpers import memory_backend
        from unittest.mock import patch

        with patch.object(plugins_helper, "get_enabled_plugins", return_value=["_memory"]), patch.object(plugins_helper, "get_plugin_config",
                          return_value={"connections": [],
                                        "memory_backend": {"enabled": False,
                                                           "connection": "",
                                                           "prefix": "mem"}}):
            self.assertIsNone(memory_backend.backend_for(object()))
            routed = await memory_backend.route_memory_tool(
                self._agent(), "save", {"text": "x"})
            self.assertIsNone(routed)

    async def test_save_search_delete_roundtrip(self):
        from usr.plugins.db_hub.helpers import memory_backend

        fake = self._install_fakes()
        agent = self._agent()
        memory_id = await memory_backend.memory_save(agent, "hello world")
        self.assertTrue(memory_id.startswith("mem_main_"))
        self.assertIn(("mem_main", 2), fake.ensured)
        stored = fake.store["mem_main"][memory_id][1]
        self.assertEqual(stored["text"], "hello world")
        self.assertEqual(stored["area"], "main")
        self.assertIn("meta", stored)
        fake.store["mem_main"] = {
            "mem_main_a": (0.9, {"text": "yes", "area": "main"}),
            "mem_main_b": (0.2, {"text": "no", "area": "main"}),
        }
        found = await memory_backend.memory_search(agent, "q", 0.7, 10)
        self.assertEqual([item["id"] for item in found], ["mem_main_a"])
        self.assertAlmostEqual(found[0]["score"], 0.9)
        deleted = await memory_backend.memory_delete(agent, ["mem_main_a"])
        self.assertEqual(deleted, 1)
        self.assertNotIn("mem_main_a", fake.store["mem_main"])

    async def test_l2_hint_normalizes(self):
        from usr.plugins.db_hub.helpers import memory_backend

        fake = self._install_fakes(driver_name="lancedb")
        agent = self._agent()
        fake.store["mem_main"] = {
            "x": (0.0, {"text": "close", "area": "main"}),
            "y": (9.0, {"text": "far", "area": "main"}),
        }
        found = await memory_backend.memory_search(agent, "q", 0.5, 10)
        self.assertEqual([item["id"] for item in found], ["x"])
        self.assertAlmostEqual(found[0]["score"], 1.0)

    async def test_areas_for_filter(self):
        from usr.plugins.db_hub.helpers.memory_backend import areas_for_filter

        self.assertEqual(areas_for_filter(""), ["main", "fragments", "solutions"])
        self.assertEqual(areas_for_filter("area=='solutions'"), ["solutions"])
        self.assertEqual(areas_for_filter("score > 3"), ["main", "fragments", "solutions"])

    async def test_to_similarity(self):
        from usr.plugins.db_hub.helpers.memory_backend import to_similarity

        self.assertAlmostEqual(to_similarity(0.8, "cosine_similarity"), 0.8)
        self.assertAlmostEqual(to_similarity(0.0, "cosine_distance"), 1.0)
        self.assertAlmostEqual(to_similarity(2.0, "cosine_distance"), 0.0)
        self.assertAlmostEqual(to_similarity(0.0, "l2_distance"), 1.0)
        self.assertIsNone(to_similarity("junk", "cosine_similarity"))

    async def test_legacy_backend_shape_converts(self):
        from usr.plugins.db_hub.helpers import memory_backend

        with unittest.mock.patch.object(
                plugins_helper, "get_plugin_config",
                return_value={"connections": [],
                              "memory_backend": {"enabled": True,
                                                 "connection": "analytics_pg",
                                                 "prefix": "mem"}}):
            targets = memory_backend.backend_targets(object())
        self.assertEqual(targets, [{"connection": "analytics_pg",
                                    "prefix": "mem"}])

    async def test_multi_backend_fanout_and_merge(self):
        import unittest.mock as mock

        from usr.plugins.db_hub.helpers import drivers as driver_registry
        from usr.plugins.db_hub.helpers import memory_backend
        from usr.plugins.db_hub.helpers.base import VectorDriver
        class FakeVector(VectorDriver):
            capabilities = {"vector"}

            def __init__(self):
                self.store = {}

            async def connect(self, conn_cfg, dsn):
                pass

            async def test(self):
                return {"ok": True}

            async def list_schema(self, target=""):
                return {}

            async def query(self, statement, params, readonly):
                raise AssertionError("not used")

            async def ensure_collection(self, collection, dim):
                self.store.setdefault(collection, {})

            async def vector_search(self, collection, vector, top_k, flt=None):
                return {"matches": [
                    {"id": mid, "_score": score, "text": text}
                    for mid, (score, text) in
                    self.store.get(collection, {}).items()]}

            async def vector_upsert(self, collection, items):
                for item in items:
                    payload = item.get("payload", {}) or {}
                    self.store.setdefault(collection, {})[item["id"]] = (
                        1.0, payload.get("text", ""))

            async def vector_delete(self, collection, ids=None, flt=None):
                store = self.store.get(collection, {})
                return {"deleted": sum(
                    1 for i in (ids or []) if store.pop(i, None) is not None)}

            async def close(self):
                pass

        primary, secondary = FakeVector(), FakeVector()
        secondary.store["mem_main"] = {
            "old_dup": (1.0, "mirrored fact"),
        }
        cfg = {
            "connections": [
                _entry(id="db_one", dsn="http://one:6333", readonly=False),
                _entry(id="db_two", dsn="http://two:6333", readonly=False),
            ],
            "max_rows": 200, "query_timeout_s": 5, "readonly_default": True,
            "memory_backend": {
                "enabled": True,
                "connections": [{"connection": "db_one"},
                                {"connection": "db_two"}],
                "prefix": "mem"},
        }
        agent = self._agent()

        with mock.patch.object(plugins_helper, "get_plugin_config",
                               return_value=cfg), \
             mock.patch.object(driver_registry, "create_driver",
                               side_effect=[primary, secondary]):
            registry._POOLS.clear()
            try:
                memory_id = await memory_backend.memory_save(agent, "mirrored fact")
                self.assertTrue(memory_id.startswith("mem_main_"))
                self.assertIn(memory_id, primary.store["mem_main"])
                self.assertIn(memory_id, secondary.store["mem_main"])
                found = await memory_backend.memory_search(agent, "q", 0.0, 10)
                ids = [item["id"] for item in found]
                self.assertIn(memory_id, ids)
                # same text on both backends dedupes to one hit
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(ids.count(memory_id), 1)
                deleted = await memory_backend.memory_delete(agent, [memory_id])
                self.assertEqual(deleted, 2)
            finally:
                registry._POOLS.clear()


class ShadowCompatTests(unittest.TestCase):
    def test_recall_shadow_matches_bundled_protocol(self):
        import ast

        from helpers.plugins import find_plugin_dir
        from helpers import files

        def _parse(path):
            tree = ast.parse(open(path).read())
            consts, funcs, classes = {}, set(), set()
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and isinstance(
                                node.value, ast.Constant):
                            consts[target.id] = node.value.value
                elif isinstance(node, ast.FunctionDef):
                    funcs.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    classes.add(node.name)
            return consts, funcs, classes

        ours = "usr/plugins/db_hub/extensions/python/message_loop_prompts_after/_50_recall_memories.py"
        bundled_dir = find_plugin_dir("_memory")
        self.assertTrue(bundled_dir, "bundled _memory not found")
        bundled = files.get_abs_path(
            bundled_dir, "extensions", "python", "message_loop_prompts_after",
            "_50_recall_memories.py")
        ours_consts, ours_funcs, ours_classes = _parse(ours)
        bundled_consts, _, _ = _parse(bundled)
        for const in ("DATA_NAME_TASK", "DATA_NAME_ITER", "DATA_NAME_LOOP",
                      "DATA_NAME_RESULT", "DATA_NAME_RESULT_SCOPE"):
            self.assertEqual(ours_consts.get(const), bundled_consts.get(const))
        for fn in ("apply_recall_result", "get_recall_scope"):
            self.assertIn(fn, ours_funcs)
        self.assertIn("RecallMemories", ours_classes)
        self.assertIn("memory_backend", open(ours).read())

class DriverRegistryTests(unittest.TestCase):
    def test_all_expected_drivers_known(self):
        self.assertEqual(
            driver_registry.driver_names(),
            ["bigquery", "chromadb", "convex", "duckdb", "fluree", "hydradb", "lancedb",
             "milvus", "mongodb", "neo4j", "pinecone", "postgres", "qdrant",
             "redis", "snowflake", "spacetimedb", "sqlalchemy", "weaviate"],
        )

    def test_unknown_driver_error_lists_valid(self):
        from usr.plugins.db_hub.helpers.base import DbHubError

        with self.assertRaisesRegex(DbHubError, "postgres"):
            driver_registry.get_driver_info("oracle")

    def test_availability_reports_without_importing_drivers(self):
        availability = driver_registry.driver_availability()
        self.assertIn("postgres", availability)
        self.assertIn("available", availability["postgres"])
        self.assertIn("capabilities", availability["postgres"])


if __name__ == "__main__":
    unittest.main()

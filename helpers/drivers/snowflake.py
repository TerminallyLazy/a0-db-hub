"""Snowflake driver: SQL over snowflake-connector-python (worker thread)."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qsl, unquote, urlparse

from usr.plugins.db_hub.helpers.base import (
    DbDriver,
    ReadOnlyError,
    is_write_statement,
)

DRIVER_NAME = "snowflake"


def _parse_dsn(dsn: str, options: dict) -> dict:
    parsed = urlparse((dsn or "").strip())
    if parsed.scheme != "snowflake":
        raise ValueError(
            "Snowflake dsn must look like "
            "snowflake://user:PASS@account/warehouse/database/schema?role=myrole"
        )
    query = dict(parse_qsl(parsed.query or ""))
    path = [p for p in (parsed.path or "").split("/") if p]
    params = {
        "account": parsed.hostname or "",
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }
    for key, slot in (("warehouse", 0), ("database", 1), ("schema", 2)):
        if len(path) > slot:
            params[key] = path[slot]
    params.update({k: v for k, v in query.items()
                   if k in {"warehouse", "database", "schema", "role"}})
    for key in ("account", "user", "password", "warehouse", "database",
                "schema", "role"):
        if options.get(key):
            params[key] = options[key]
    if options.get('oauth_access_token'):
        params.pop('password', None)
        params.update(authenticator='oauth', token=options['oauth_access_token'])
    if not params.get("account") or (not params.get("user") and not options.get('oauth_access_token')):
        raise ValueError("Snowflake needs at least account and user.")
    return params


class SnowflakeDriver(DbDriver):
    capabilities: set[str] = {"sql"}

    def __init__(self):
        self._params: dict = {}
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import snowflake.connector  # noqa: F401  (probed import)

        self._options = dict(conn_cfg or {})
        try:
            self._params = _parse_dsn(resolved_dsn, self._options)
        except ValueError as exc:
            from usr.plugins.db_hub.helpers.base import DbHubError

            raise DbHubError(str(exc)) from exc

    def _connection(self):
        import snowflake.connector

        return snowflake.connector.connect(**self._params)

    async def test(self) -> dict:
        def _run():
            conn = self._connection()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT CURRENT_VERSION()")
                    return cursor.fetchone()
                finally:
                    cursor.close()
            finally:
                conn.close()

        t0 = time.monotonic()
        row = await asyncio.to_thread(_run)
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "version": row[0] if row else "",
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        database = self._params.get("database", "")
        schema = self._params.get("schema", "")

        def _run():
            conn = self._connection()
            try:
                cursor = conn.cursor()
                try:
                    if target:
                        parts = target.split(".")
                        table = parts[-1].upper()
                        sch = parts[-2].upper() if len(parts) > 1 else schema.upper()
                        cursor.execute(
                            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
                            "FROM INFORMATION_SCHEMA.COLUMNS "
                            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                            "ORDER BY ORDINAL_POSITION", (sch, table))
                        cols = cursor.fetchall()
                        return {"table": target,
                                "columns": [{"name": c[0], "type": c[1],
                                             "nullable": c[2]} for c in cols]}
                    cursor.execute(
                        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE "
                        "FROM INFORMATION_SCHEMA.TABLES "
                        "WHERE TABLE_SCHEMA NOT IN "
                        "('INFORMATION_SCHEMA') ORDER BY 1, 2 LIMIT 500")
                    return {"tables": [
                        {"schema": r[0], "table": r[1], "type": r[2]}
                        for r in cursor.fetchall()],
                        "database": database,
                        "hint": "Pass target 'SCHEMA.TABLE' for columns."}
                finally:
                    cursor.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        if readonly and is_write_statement(statement):
            raise ReadOnlyError("Snowflake connection is read-only.")

        def _run():
            conn = self._connection()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute(statement, params or None)
                    if cursor.description:
                        columns = [d[0] for d in cursor.description]
                        rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
                        return {"columns": columns, "rows": rows,
                                "rowcount": len(rows)}
                    return {"columns": [], "rows": [],
                            "rowcount": cursor.rowcount}
                finally:
                    cursor.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        self._params = {}


def create() -> SnowflakeDriver:
    return SnowflakeDriver()

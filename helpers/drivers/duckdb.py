"""DuckDB driver: embedded analytical SQL (file or :memory:)."""

from __future__ import annotations

import asyncio
import time

from usr.plugins.db_hub.helpers.base import (
    DbDriver,
    DbHubError,
    ReadOnlyError,
    is_write_statement,
)

DRIVER_NAME = "duckdb"


class DuckDriver(DbDriver):
    capabilities: set[str] = {"sql"}

    def __init__(self):
        self._conn = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import duckdb

        self._options = dict(conn_cfg or {})
        if self._conn is not None:
            return
        path = (resolved_dsn or "").strip() or ":memory:"
        self._conn = await asyncio.to_thread(duckdb.connect, path)

    async def test(self) -> dict:
        t0 = time.monotonic()
        version = await asyncio.to_thread(
            lambda: self._conn.execute("SELECT version()").fetchone()[0])
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "version": version,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            if target:
                cols = self._conn.execute(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = ? ORDER BY ordinal_position",
                    [target]).fetchall()
                return {"table": target,
                        "columns": [{"name": c[0], "type": c[1],
                                     "nullable": c[2]} for c in cols]}
            tables = self._conn.execute(
                "SELECT table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN "
                "('pg_catalog','information_schema') "
                "ORDER BY table_schema, table_name").fetchall()
            return {"tables": [{"schema": t[0], "table": t[1], "type": t[2]}
                               for t in tables],
                    "hint": "Pass target '<table>' for columns."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        if readonly and is_write_statement(statement):
            raise ReadOnlyError("DuckDB connection is read-only.")

        def _run():
            cursor = self._conn.execute(statement, params or {})
            if cursor.description:
                columns = [d[0] for d in cursor.description]
                rows = [dict(zip(columns, r)) for r in cursor.fetchall()]
                return {"columns": columns, "rows": rows,
                        "rowcount": len(rows)}
            return {"columns": [], "rows": [], "rowcount": cursor.rowcount}

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await asyncio.to_thread(self._conn.close)
            except Exception:
                pass
            self._conn = None


def create() -> DuckDriver:
    return DuckDriver()

"""Generic SQL driver via SQLAlchemy (catch-all for other databases)."""

from __future__ import annotations

import asyncio

from usr.plugins.db_hub.helpers.base import (
    DbDriver,
    DbHubError,
    ReadOnlyError,
    is_write_statement,
)

DRIVER_NAME = "sqlalchemy"


class SqlAlchemyDriver(DbDriver):
    capabilities: set[str] = {"sql"}

    def __init__(self):
        self._engine = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        from sqlalchemy import create_engine

        self._options = dict(conn_cfg or {})
        if self._engine is not None:
            return
        dsn = (resolved_dsn or "").strip()
        if not dsn:
            raise DbHubError("SQLAlchemy driver needs a database URL as dsn.")
        kwargs: dict = {}
        if self._options.get("connect_args"):
            kwargs["connect_args"] = self._options["connect_args"]
        self._engine = await asyncio.to_thread(create_engine, dsn, **kwargs)

    async def test(self) -> dict:
        import time

        from sqlalchemy import text

        def _run():
            with self._engine.connect() as conn:
                version = conn.execute(text("SELECT 1")).scalar()
            return version

        t0 = time.monotonic()
        value = await asyncio.to_thread(_run)
        return {
            "ok": value == 1,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "driver": DRIVER_NAME,
            "dialect": self._engine.dialect.name,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            from sqlalchemy import inspect as sa_inspect

            inspector = sa_inspect(self._engine)
            if target:
                schema = None
                table = target
                if "." in target:
                    schema, table = target.rsplit(".", 1)
                cols = inspector.get_columns(table, schema=schema)
                return {"table": target,
                        "columns": [{"name": c["name"], "type": str(c["type"]),
                                     "nullable": c.get("nullable")} for c in cols]}
            names = inspector.get_table_names()
            return {"tables": names,
                    "hint": "Pass target '<table>' for columns."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        from sqlalchemy import text

        if readonly and is_write_statement(statement):
            raise ReadOnlyError("SQL connection is read-only.")

        def _run():
            with self._engine.begin() as conn:
                result = conn.execute(text(statement), params or {})
                if result.returns_rows:
                    columns = list(result.keys())
                    rows = [dict(r._mapping) for r in result.fetchall()]
                    return {"columns": columns, "rows": rows,
                            "rowcount": len(rows)}
                return {"columns": [], "rows": [],
                        "rowcount": result.rowcount}

        return await asyncio.to_thread(_run)

    async def close(self) -> None:
        if self._engine is not None:
            try:
                await asyncio.to_thread(self._engine.dispose)
            except Exception:
                pass
            self._engine = None


def create() -> SqlAlchemyDriver:
    return SqlAlchemyDriver()

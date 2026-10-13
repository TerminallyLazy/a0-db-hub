"""LanceDB driver: embedded or cloud vector tables."""

from __future__ import annotations

import asyncio
import time

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "lancedb"
VECTOR_COLUMN = "vector"


class LanceDriver(VectorDriver):
    capabilities: set[str] = {"vector"}

    def __init__(self):
        self._db = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import lancedb

        self._options = dict(conn_cfg or {})
        if self._db is not None:
            return
        uri = (resolved_dsn or "").strip() or "~/.lancedb"
        kwargs: dict = {}
        if self._options.get("api_key"):
            kwargs["api_key"] = self._options["api_key"]
        if self._options.get("region"):
            kwargs["region"] = self._options["region"]
        if self._options.get("host_override"):
            kwargs["host_override"] = self._options["host_override"]
        self._db = await asyncio.to_thread(lancedb.connect, uri, **kwargs)

    def _table(self, collection: str):
        try:
            return self._db.open_table(collection)
        except Exception as exc:
            raise DbHubError(
                f"LanceDB table '{collection}' does not exist ({exc}). "
                "Create it first or set options.create_table=true for upserts."
            ) from exc

    async def test(self) -> dict:
        t0 = time.monotonic()
        names = await asyncio.to_thread(self._db.table_names)
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "tables": names,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        if target:
            table = self._table(target)
            schema = await asyncio.to_thread(lambda: str(table.schema))
            count = await asyncio.to_thread(table.count_rows)
            return {"table": target, "schema": schema, "rows": count}
        names = await asyncio.to_thread(self._db.table_names)
        return {"tables": names, "hint": "Pass target '<table>' for schema."}

    async def browse(self, collection, limit=50, cursor=None):
        offset = max(0, min(int(cursor or 0), 100000))
        def read():
            rows = self._table(collection).search().offset(offset).limit(limit).to_list()
            # A preview shows document fields, without returning large vectors.
            for row in rows: row.pop(self._options.get("vector_column", "vector"), None)
            return {"rows": rows, "next_cursor": offset + limit if len(rows) == limit else None}
        return await asyncio.to_thread(read)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        raise DbHubError(
            "LanceDB is vector-only in v1; use db_vector search/upsert/delete."
        )

    def _where(self, flt: dict | None) -> str | None:
        if not flt:
            return None
        if flt.get("sql"):
            return str(flt["sql"])
        clauses = []
        for field, value in flt.items():
            if isinstance(value, str):
                escaped = value.replace("'", "''")
                clauses.append(f"{field} = '{escaped}'")
            elif isinstance(value, bool):
                clauses.append(f"{field} = {str(value).lower()}")
            elif value is None:
                clauses.append(f"{field} IS NULL")
            else:
                clauses.append(f"{field} = {value}")
        return " AND ".join(clauses) if clauses else None

    async def ensure_collection(self, collection: str, dim: int) -> None:
        import pyarrow as pa

        schema = pa.schema([
            ("id", pa.string()),
            ("text", pa.string()),
            ("vector", pa.list_(pa.float32(), max(int(dim), 1))),
            ("meta", pa.string()),
            ("area", pa.string()),
            ("timestamp", pa.string()),
            ("memory_id", pa.string()),
        ])

        def _run():
            try:
                self._db.open_table(collection)
            except Exception:
                self._db.create_table(collection, schema=schema)

        await asyncio.to_thread(_run)

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        table = self._table(collection)

        def _run():
            query = table.search([float(x) for x in vector]).limit(
                max(1, min(int(top_k or 10), 100)))
            where = self._where(flt)
            if where:
                query = query.where(where)
            return query.to_list()

        rows = await asyncio.to_thread(_run)
        for row in rows:
            row.pop(VECTOR_COLUMN, None)
        return {"matches": rows}

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        rows = []
        for item in items:
            row = dict(item.get("payload", {}) or {})
            if "text" in item and "text" not in row:
                row["text"] = item["text"]
            if "id" in item and "id" not in row:
                row["id"] = item["id"]
            row[VECTOR_COLUMN] = [float(x) for x in item["vector"]]
            rows.append(row)
        if not rows:
            return {"upserted": 0}

        def _run():
            try:
                table = self._db.open_table(collection)
                table.add(rows)
            except Exception:
                if not self._options.get("create_table"):
                    raise
                self._db.create_table(collection, rows)
            return len(rows)

        try:
            count = await asyncio.to_thread(_run)
        except Exception as exc:
            raise DbHubError(f"LanceDB upsert failed: {exc}") from exc
        return {"upserted": count}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        if not ids and not flt:
            raise DbHubError("LanceDB delete needs ids or a filter.")
        if flt and not ids:
            where = self._where(flt)
            if not where:
                raise DbHubError("LanceDB delete needs ids or a filter.")
        else:
            quoted = ", ".join(
                "'" + str(i).replace("'", "''") + "'"
                for i in (ids or []))
            where = f"id in ({quoted})"

        def _run():
            table = self._table(collection)
            table.delete(where)

        await asyncio.to_thread(_run)
        return {"deleted": len(ids) if ids else "filter"}

    async def close(self) -> None:
        self._db = None


def create() -> LanceDriver:
    return LanceDriver()

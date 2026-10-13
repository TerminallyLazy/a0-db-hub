"""Postgres-wire driver (Postgres, Neon, Supabase): SQL + pgvector."""

from __future__ import annotations

import re
import time
import json
import math

from usr.plugins.db_hub.helpers.base import (
    DbHubError,
    ReadOnlyError,
    VectorDriver,
    is_write_statement,
)

DRIVER_NAME = "postgres"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_NAMED_PARAM_RE = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def _vector_text(vector: list[float]) -> str:
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise DbHubError("Vectors must contain finite numbers and cannot be empty.")
    return json.dumps(values, allow_nan=False)


def _ident(name: str, what: str = "identifier") -> str:
    if not _IDENT_RE.match(name or ""):
        raise DbHubError(f"Unsafe Postgres {what}: '{name}'.")
    return name


def _rewrite_params(statement: str, params: dict) -> tuple[str, list]:
    """Rewrite :name placeholders to asyncpg $n args (ordered by appearance)."""
    if not params:
        return statement, []
    if "$1" in statement:
        ordered = sorted(params, key=lambda k: int(k) if str(k).isdigit() else 0)
        return statement, [params[k] for k in ordered]
    names: list[str] = []

    def _sub(match: re.Match) -> str:
        names.append(match.group(1))
        return f"${len(names)}"

    rewritten = _NAMED_PARAM_RE.sub(_sub, statement)
    missing = [n for n in names if n not in params]
    if missing:
        raise DbHubError(f"Missing query params: {', '.join(missing)}.")
    return rewritten, [params[n] for n in names]


class PostgresDriver(VectorDriver):
    capabilities: set[str] = {"sql", "vector"}

    def __init__(self):
        self._pool = None
        self._options: dict = {}
        self._timeout = 30.0

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import asyncpg

        self._options = dict(conn_cfg or {})
        self._timeout = float(self._options.get("command_timeout", 30) or 30)
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            resolved_dsn, min_size=1, max_size=5,
            command_timeout=self._timeout,
            statement_cache_size=int(self._options.get('statement_cache_size', 100)),
        )

    async def test(self) -> dict:
        t0 = time.monotonic()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT version() AS version")
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "version": (row or {}).get("version", ""),
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        async with self._pool.acquire() as conn:
            if target:
                schema, _, table = target.partition(".")
                if not table:
                    table, schema = schema, "public"
                cols = await conn.fetch(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema=$1 AND table_name=$2 "
                    "ORDER BY ordinal_position",
                    schema or "public", table,
                )
                return {"table": target, "columns": [dict(c) for c in cols]}
            tables = await conn.fetch(
                "SELECT table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
                "ORDER BY table_schema, table_name"
            )
            return {
                "tables": [dict(t) for t in tables],
                "hint": "Pass target 'schema.table' for columns.",
            }

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        if readonly and is_write_statement(statement):
            raise ReadOnlyError("Postgres connection is read-only.")
        text, args = _rewrite_params(statement, params or {})
        async with self._pool.acquire() as conn:
            if readonly:
                async with conn.transaction(readonly=True):
                    rows = await conn.fetch(text, *args)
                return {'columns': list(rows[0].keys()) if rows else [],
                        'rows': [dict(r) for r in rows], 'rowcount': len(rows)}
            if not is_write_statement(statement):
                rows = await conn.fetch(text, *args)
                return {
                    "columns": list(rows[0].keys()) if rows else [],
                    "rows": [dict(r) for r in rows],
                    "rowcount": len(rows),
                }
            status = await conn.execute(text, *args)
            return {"columns": [], "rows": [], "rowcount": None, "status": status}

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        table = _ident(collection, "table")
        opts = self._options
        column = _ident(str(opts.get("vector_column", "embedding")), "column")
        op = str(opts.get("distance_op", "<=>"))
        if op not in {"<=>", "<#>", "<->"}:
            raise DbHubError("distance_op must be one of <=>, <#>, <->.")
        where = ""
        args: list = [_vector_text(vector)]
        if flt and isinstance(flt, dict) and flt.get("sql"):
            where = f"WHERE {flt['sql']}"
            args.extend(flt.get("args", []) or [])
        sql = (
            f"SELECT *, {column} {op} $1::text::vector AS _distance FROM {table} "
            f"{where} ORDER BY _distance LIMIT {max(1, min(int(top_k or 10), 100))}"
        )
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except Exception as exc:
            if getattr(exc, "sqlstate", "") in {"42P01", "42704"}:
                raise DbHubError(f"Postgres memory table '{table}' or vector type does not exist.") from exc
            raise
        matches = []
        for row in rows:
            item = dict(row)
            item.pop(column, None)
            item["id"] = str(item.pop(str(opts.get("id_column", "id")), ""))
            metadata = item.pop(str(opts.get("metadata_column", "metadata")), None)
            if isinstance(metadata, dict):
                item.update(metadata)
            elif isinstance(metadata, str):
                import json as _json

                try:
                    item.update(_json.loads(metadata))
                except ValueError:
                    pass
            matches.append(item)
        return {"matches": matches}

    async def ensure_collection(self, collection: str, dim: int) -> None:
        table = _ident(collection, "table")
        id_col = _ident(str(self._options.get("id_column", "id")), "column")
        vec_col = _ident(str(self._options.get("vector_column", "embedding")), "column")
        meta_col = _ident(str(self._options.get("metadata_column", "metadata")), "column")
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                f"{id_col} TEXT PRIMARY KEY, "
                f"{vec_col} vector({max(int(dim), 1)}) NOT NULL, "
                f"{meta_col} JSONB NOT NULL DEFAULT '{{}}')")

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        table = _ident(collection, "table")
        opts = self._options
        id_col = _ident(str(opts.get("id_column", "id")), "column")
        vec_col = _ident(str(opts.get("vector_column", "embedding")), "column")
        meta_col = _ident(str(opts.get("metadata_column", "metadata")), "column")
        import json as _json

        async with self._pool.acquire() as conn:
            count = 0
            for item in items:
                payload = item.get("payload", {}) or {}
                if "id" in item and "payload" not in item:
                    payload = {k: v for k, v in item.items()
                               if k not in {"id", "vector"}}
                await conn.execute(
                    f"INSERT INTO {table} ({id_col}, {vec_col}, {meta_col}) "
                    f"VALUES ($1, $2::text::vector, $3::jsonb) "
                    f"ON CONFLICT ({id_col}) DO UPDATE SET {vec_col}=EXCLUDED.{vec_col}, "
                    f"{meta_col}=EXCLUDED.{meta_col}",
                    str(item.get("id", "")) or None,
                    _vector_text(item["vector"]),
                    _json.dumps(payload),
                )
                count += 1
        return {"upserted": count}

    async def vector_delete(self, collection: str, ids=None, flt=None) -> dict:
        if not ids or flt:
            raise DbHubError("Postgres vector delete requires explicit ids.")
        table = _ident(collection, "table")
        id_col = _ident(str(self._options.get("id_column", "id")), "column")
        try:
            async with self._pool.acquire() as conn:
                status = await conn.execute(
                    f"DELETE FROM {table} WHERE {id_col} = ANY($1::text[])",
                    [str(item) for item in ids])
        except Exception as exc:
            if getattr(exc, "sqlstate", "") == "42P01":
                raise DbHubError(f"Postgres memory table '{table}' does not exist.") from exc
            raise
        return {"deleted": int(status.split()[-1])}

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                pass
            self._pool = None


def create() -> PostgresDriver:
    return PostgresDriver()

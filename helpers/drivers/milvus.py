"""Milvus driver: vector collections over MilvusClient (sync, in thread)."""

from __future__ import annotations

import asyncio
import time
import uuid

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "milvus"


class MilvusDriver(VectorDriver):
    capabilities: set[str] = {"vector"}

    def __init__(self):
        self._client = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        from pymilvus import MilvusClient

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        dsn = (resolved_dsn or "").strip() or "http://localhost:19530"
        kwargs: dict = {"uri": dsn}
        if self._options.get("token"):
            kwargs["token"] = self._options["token"]
        if self._options.get("user"):
            kwargs["user"] = self._options["user"]
        if self._options.get("password"):
            kwargs["password"] = self._options["password"]
        self._client = await asyncio.to_thread(MilvusClient, **kwargs)

    def _expr(self, flt: dict | None) -> str:
        if not flt:
            return ""
        if flt.get("expr"):
            return str(flt["expr"])
        clauses = []
        for field, value in flt.items():
            if field in {"sql", "args"}:
                continue
            if isinstance(value, str):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                clauses.append(f'{field} == "{escaped}"')
            elif isinstance(value, bool):
                clauses.append(f"{field} == {str(value).lower()}")
            else:
                clauses.append(f"{field} == {value}")
        return " && ".join(clauses)

    async def test(self) -> dict:
        t0 = time.monotonic()
        names = await asyncio.to_thread(self._client.list_collections)
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "collections": names,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            if target:
                info = self._client.describe_collection(target)
                return {"collection": target, "info": str(info)}
            detail = {}
            for name in self._client.list_collections():
                try:
                    info = self._client.describe_collection(name)
                    detail[name] = {"fields": [
                        f.get("name") for f in info.get("fields", [])]}
                except Exception:
                    detail[name] = {}
            return {"collections": detail,
                    "hint": "Pass target '<collection>' for details."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        raise DbHubError(
            "Milvus is vector-only; use db_vector search/upsert/delete.")

    def _vector_field(self, collection: str) -> str:
        return str(self._options.get("vector_field", "vector"))

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        def _run():
            result = self._client.search(
                collection_name=collection,
                data=[[float(x) for x in vector]],
                limit=max(1, min(int(top_k or 10), 100)),
                filter=self._expr(flt) or None,
                output_fields=["*"],
            )
            matches = []
            for hits in result:
                for hit in hits:
                    entity = dict(hit.get("entity", {}) or {})
                    entity.pop(self._vector_field(collection), None)
                    matches.append({"id": hit.get("id"),
                                    "_distance": hit.get("distance"),
                                    **entity})
            return {"matches": matches}

        return await asyncio.to_thread(_run)

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        field = self._vector_field(collection)

        def _run():
            rows = []
            for item in items:
                row = dict(item.get("payload", {}) or {})
                if "text" in item and "text" not in row:
                    row["text"] = item["text"]
                row[field] = [float(x) for x in item["vector"]]
                if item.get("id") is not None and "id" not in row:
                    row["id"] = item["id"]
                rows.append(row)
            result = self._client.insert(collection_name=collection, data=rows)
            return result.get("insert_count", len(rows))

        return {"upserted": await asyncio.to_thread(_run)}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        id_field = str(self._options.get("id_field", "id"))

        def _run():
            if ids:
                quoted = ", ".join(
                    f'"{i}"' if isinstance(i, str) else str(i) for i in ids)
                expr = f"{id_field} in [{quoted}]"
            elif flt:
                expr = self._expr(flt)
                if not expr:
                    raise DbHubError("Milvus delete needs ids or a filter.")
            else:
                raise DbHubError("Milvus delete needs ids or a filter.")
            result = self._client.delete(collection_name=collection, filter=expr)
            return result.get("delete_count", "?")

        return {"deleted": await asyncio.to_thread(_run)}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
            self._client = None


def create() -> MilvusDriver:
    return MilvusDriver()

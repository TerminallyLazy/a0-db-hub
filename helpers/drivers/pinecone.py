"""Pinecone driver: serverless/pod vector indexes (v3 client)."""

from __future__ import annotations

import asyncio
import time
import uuid

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "pinecone"


class PineconeDriver(VectorDriver):
    capabilities: set[str] = {"vector"}

    def __init__(self):
        self._client = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        from pinecone import Pinecone

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        api_key = str(self._options.get("api_key", "") or "")
        if not api_key:
            raise DbHubError(
                "Pinecone needs options.api_key (use a §§secret() placeholder).")
        self._client = Pinecone(api_key=api_key)

    async def _get_index(self, collection: str):
        def _run():
            host = str(self._options.get("host", "") or "")
            if not host:
                host = self._client.describe_index(collection).host
            return self._client.Index(host=host)

        return await asyncio.to_thread(_run)

    async def test(self) -> dict:
        t0 = time.monotonic()

        def _run():
            return [i.name for i in self._client.list_indexes()]

        names = await asyncio.to_thread(_run)
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "indexes": names,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            if target:
                stats = self._client.Index(
                    host=self._options.get("host") or
                    self._client.describe_index(target).host).describe_index_stats()
                return {"index": target,
                        "vector_count": stats.get("total_vector_count"),
                        "dimension": stats.get("dimension")}
            return {"indexes": [i.name for i in self._client.list_indexes()],
                    "hint": "Pass target '<index>' for stats."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        raise DbHubError(
            "Pinecone is vector-only; use db_vector search/upsert/delete.")

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        index = await self._get_index(collection)

        def _run():
            kwargs: dict = {
                "vector": [float(x) for x in vector],
                "top_k": max(1, min(int(top_k or 10), 100)),
                "include_metadata": True,
            }
            if flt:
                kwargs["filter"] = {k: {"$eq": v} for k, v in flt.items()
                                    if k not in {"sql", "args"}}
            return index.query(**kwargs).to_dict()

        result = await asyncio.to_thread(_run)
        matches = []
        for hit in result.get("matches", []) or []:
            matches.append({"id": hit.get("id"), "_score": hit.get("score"),
                            **(hit.get("metadata", {}) or {})})
        return {"matches": matches}

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        index = await self._get_index(collection)
        vectors = []
        for item in items:
            payload = dict(item.get("payload", {}) or {})
            if "text" in item and "text" not in payload:
                payload["text"] = item["text"]
            vectors.append({"id": str(item.get("id") or uuid.uuid4()),
                            "values": [float(x) for x in item["vector"]],
                            "metadata": payload})

        def _run():
            for i in range(0, len(vectors), 100):
                index.upsert(vectors=vectors[i:i + 100])

        await asyncio.to_thread(_run)
        return {"upserted": len(vectors)}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        if not ids and not flt:
            raise DbHubError("Pinecone delete needs ids or a filter.")
        index = await self._get_index(collection)

        def _run():
            if ids:
                index.delete(ids=[str(i) for i in ids])
            else:
                index.delete(filter={k: {"$eq": v} for k, v in flt.items()})

        await asyncio.to_thread(_run)
        return {"deleted": len(ids) if ids else "filter"}

    async def close(self) -> None:
        self._client = None


def create() -> PineconeDriver:
    return PineconeDriver()

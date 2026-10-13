"""ChromaDB driver: persistent-local or client/server vector collections."""

from __future__ import annotations

import asyncio
import time
import uuid

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "chromadb"


class ChromaDriver(VectorDriver):
    capabilities: set[str] = {"vector"}

    def __init__(self):
        self._client = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        dsn = (resolved_dsn or "").strip()

        def _run():
            if dsn.startswith("http"):
                import chromadb

                from urllib.parse import urlparse

                parsed = urlparse(dsn)
                ssl = parsed.scheme == "https"
                return chromadb.HttpClient(
                    host=parsed.hostname or "localhost",
                    port=parsed.port or (443 if ssl else 8000),
                    ssl=ssl,
                    headers={"Authorization": f"Bearer {self._options['api_key']}"}
                    if self._options.get("api_key") else None,
                )
            import chromadb

            return chromadb.PersistentClient(path=dsn or "./chromadb")

        self._client = await asyncio.to_thread(_run)

    def _collection(self, collection: str, create: bool = False):
        try:
            return self._client.get_collection(collection)
        except Exception as exc:
            if not create and not self._options.get("create_collection"):
                raise DbHubError(
                    f"Chroma collection '{collection}' does not exist. "
                    "Set options.create_collection=true to auto-create."
                ) from exc
            metadata = {"hnsw:space": str(self._options.get("space", "cosine"))}
            return self._client.get_or_create_collection(
                collection, metadata=metadata)

    async def test(self) -> dict:
        t0 = time.monotonic()
        names = await asyncio.to_thread(
            lambda: [c.name for c in self._client.list_collections()])
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "collections": names,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            if target:
                collection = self._collection(target)
                return {"collection": target, "count": collection.count(),
                        "metadata": collection.metadata}
            return {"collections": [
                {"name": c.name, "count": c.count()}
                for c in self._client.list_collections()],
                "hint": "Pass target '<collection>' for details."}

        return await asyncio.to_thread(_run)

    async def browse(self, collection, limit=50, cursor=None):
        offset = max(0, min(int(cursor or 0), 100000))
        def read():
            result = self._client.get_collection(collection).get(limit=limit, offset=offset, include=["documents", "metadatas"])
            ids = result.get("ids", [])
            rows = [{"id": item, "text": (result.get("documents") or [None] * len(ids))[idx], "metadata": (result.get("metadatas") or [None] * len(ids))[idx]} for idx, item in enumerate(ids)]
            return {"rows": rows, "next_cursor": offset + limit if len(rows) == limit else None}
        return await asyncio.to_thread(read)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        raise DbHubError(
            "ChromaDB is vector-only; use db_vector search/upsert/delete.")

    async def ensure_collection(self, collection: str, dim: int) -> None:
        await asyncio.to_thread(self._collection, collection, True)

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        def _run():
            coll = self._collection(collection)
            where = {k: v for k, v in (flt or {}).items()
                     if k not in {"sql", "args"}} or None
            result = coll.query(
                query_embeddings=[[float(x) for x in vector]],
                n_results=max(1, min(int(top_k or 10), 100)),
                where=where)
            matches = []
            for i, doc_id in enumerate(result.get("ids", [[]])[0]):
                meta = (result.get("metadatas", [[]])[0] or [{}])[i] or {}
                dist = (result.get("distances", [[]])[0] or [None])[i]
                matches.append({"id": doc_id, "_distance": dist, **meta})
            return {"matches": matches}

        return await asyncio.to_thread(_run)

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        def _run():
            coll = self._collection(collection, create=True)
            ids, embeddings, metadatas, documents = [], [], [], []
            for item in items:
                ids.append(str(item.get("id") or uuid.uuid4()))
                embeddings.append([float(x) for x in item["vector"]])
                payload = dict(item.get("payload", {}) or {})
                text = item.get("text", payload.get("text", ""))
                documents.append(str(text or payload.get("id", ids[-1])))
                metadatas.append(payload)
            coll.upsert(ids=ids, embeddings=embeddings,
                        metadatas=metadatas, documents=documents)
            return len(ids)

        return {"upserted": await asyncio.to_thread(_run)}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        if not ids and not flt:
            raise DbHubError("Chroma delete needs ids or a where filter.")

        def _run():
            coll = self._collection(collection)
            if ids:
                coll.delete(ids=[str(i) for i in ids])
                return len(ids)
            coll.delete(where={k: v for k, v in flt.items()
                               if k not in {"sql", "args"}})
            return "filter"

        return {"deleted": await asyncio.to_thread(_run)}

    async def close(self) -> None:
        self._client = None


def create() -> ChromaDriver:
    return ChromaDriver()

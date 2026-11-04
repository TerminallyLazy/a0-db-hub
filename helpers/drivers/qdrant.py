"""Qdrant driver: vector collections (sync client run in a worker thread)."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
import threading

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "qdrant"

# Embedded Qdrant exclusively locks its directory. Setup hooks, connection tests
# and agent loops may coexist in one framework process; share its local handle.
_LOCAL_CLIENTS = {}
_LOCAL_GUARD = threading.RLock()


def point_id(raw_id):
    """Qdrant accepts uint64/UUID only; map logical memory ids reversibly via payload."""
    text = str(raw_id)
    if text.isdigit() and 0 <= int(text) < 2**64:
        return int(text)
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "db_hub:" + text))


class QdrantDriver(VectorDriver):
    async def browse(self, collection, limit=50, cursor=None):
        records, next_cursor = await self._run(self._client.scroll,
            collection_name=collection,limit=limit,offset=cursor,with_payload=True,with_vectors=False)
        return {'rows':[{'id':str(record.id), **(record.payload or {})} for record in records],
                'next_cursor':next_cursor}

    capabilities: set[str] = {"vector"}

    def __init__(self):
        self._client = None
        self._options: dict = {}
        self._local_key = None
        self._local_lock = None

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        from qdrant_client import QdrantClient

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        dsn = (resolved_dsn or "").strip()
        if dsn.startswith("http"):
            self._client = QdrantClient(
                url=dsn, api_key=self._options.get("api_key") or None,
                timeout=float(self._options.get("timeout_s", 30) or 30),
            )
        elif dsn.startswith("qdrant://"):
            self._client = QdrantClient(
                url="http://" + dsn[len("qdrant://"):],
                api_key=self._options.get("api_key") or None,
            )
        elif dsn:
            key = str(Path(dsn).expanduser().resolve())
            with _LOCAL_GUARD:
                item = _LOCAL_CLIENTS.get(key)
                if item is None:
                    item = {'client': QdrantClient(path=key), 'refs': 0, 'lock': threading.RLock()}
                    _LOCAL_CLIENTS[key] = item
                item['refs'] += 1
                self._client = item['client']
                self._local_key, self._local_lock = key, item['lock']
        else:
            raise DbHubError("Qdrant dsn must be an http(s):// URL or a local path.")

    def _run(self, func, *args, **kwargs):
        def run():
            if self._local_lock is not None:
                with self._local_lock:
                    return func(*args, **kwargs)
            return func(*args, **kwargs)
        return asyncio.to_thread(run)

    async def _guarded(self, coro_fn, not_found_msg=""):
        """Run a client call, translating HTTP errors into guidance."""
        try:
            return await coro_fn()
        except Exception as exc:
            status = (
                getattr(exc, "status_code", None)
                or getattr(exc, "status", None)
                or getattr(exc, "code", None)
            )
            text = str(exc)
            if status is None:
                import re as _re

                match = _re.search(r"\b(401|403|404)\b", text)
                status = int(match.group(1)) if match else None
            if status in (401, 403):
                raise DbHubError(
                    "Qdrant refused the request ("
                    f"{status or 'auth error'}). This usually means a missing "
                    "or wrong API key: set options.api_key (the API key field "
                    "in settings, preferably as §§secret(KEY)). Qdrant Cloud "
                    "always requires one."
                ) from exc
            if status == 404 and not_found_msg:
                raise DbHubError(not_found_msg) from exc
            raise

    async def test(self) -> dict:
        t0 = time.monotonic()
        collections = await self._guarded(
            lambda: self._run(self._client.get_collections))
        names = [c.name for c in collections.collections]
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "collections": names,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        if target:
            info = await self._guarded(
                lambda: self._run(self._client.get_collection, target),
                f"Qdrant collection '{target}' does not exist.")
            return {"collection": target, "info": str(info.config),
                    "points_count": getattr(info, "points_count", None)}
        collections = await self._guarded(
            lambda: self._run(self._client.get_collections))
        detail = {}
        for collection in collections.collections:
            info = await self._guarded(
                lambda: self._run(self._client.get_collection, collection.name),
                f"Qdrant collection '{collection.name}' does not exist.")
            detail[collection.name] = {
                "points_count": getattr(info, "points_count", None),
                "vectors": str(getattr(getattr(info, "config", None), "params", None)),
            }
        return {"collections": detail,
                "hint": "Pass target '<collection>' for details."}

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        raise DbHubError(
            "Qdrant is vector-only; use db_vector search/upsert/delete, "
            "not db_query."
        )

    def _filter(self, flt: dict | None):
        if not flt:
            return None
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        must = []
        for field, value in flt.items():
            if field in {"sql", "args"}:
                continue
            must.append(FieldCondition(key=field, match=MatchValue(value=value)))
        return Filter(must=must) if must else None

    async def ensure_collection(self, collection: str, dim: int) -> None:
        from qdrant_client.http.models import Distance, VectorParams

        def _run():
            if self._client.collection_exists(collection):
                info = self._client.get_collection(collection)
                vectors = info.config.params.vectors
                if getattr(vectors, "size", None) != dim or getattr(vectors, 'distance', None) != Distance.COSINE:
                    raise DbHubError(
                        f"Qdrant collection '{collection}' must use an unnamed cosine vector of dimension {dim}. "
                        "Choose a new memory prefix when changing embedding models.")
                return "exists"
            self._client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(
                    size=max(int(dim), 1), distance=Distance.COSINE),
            )
            return "created"

        await self._guarded(lambda: self._run(_run))

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        query_filter = self._filter(flt)
        points = await self._guarded(
            lambda: self._run(
                self._search, collection, [float(x) for x in vector],
                max(1, min(int(top_k or 10), 100)), query_filter),
            f"Qdrant collection '{collection}' does not exist.")
        return {"matches": [
            {**(p.payload or {}), "id": str((p.payload or {}).get("memory_id") or p.id),
             "score": p.score, "_score": p.score} for p in points
        ]}

    def _search(self, collection, vector, top_k, query_filter):
        client = self._client
        if hasattr(client, "query_points"):
            return client.query_points(
                collection_name=collection, query=vector, limit=top_k,
            query_filter=query_filter, with_payload=True).points
        return client.search(
            collection_name=collection, query_vector=vector, limit=top_k,
            query_filter=query_filter, with_payload=True)

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        from qdrant_client.http.models import PointStruct

        points = []
        for item in items:
            raw_id = item.get("id") or str(uuid.uuid4())
            payload = dict(item.get("payload", {}) or {})
            if "text" in item and "text" not in payload:
                payload["text"] = item["text"]
            points.append(PointStruct(
                id=point_id(raw_id), vector=[float(x) for x in item["vector"]],
                payload=payload))
        await self._guarded(
            lambda: self._run(self._client.upsert, collection_name=collection,
                              points=points, wait=True),
            f"Qdrant collection '{collection}' does not exist.")
        return {"upserted": len(points)}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        from qdrant_client.http.models import FilterSelector, PointIdsList

        if ids:
            selector: object = PointIdsList(points=[point_id(item) for item in ids])
        elif flt:
            query_filter = self._filter(flt)
            if query_filter is None:
                raise DbHubError("vector delete needs ids or a field filter.")
            selector = FilterSelector(filter=query_filter)
        else:
            raise DbHubError("vector delete needs ids or a filter.")
        await self._guarded(
            lambda: self._run(
                self._client.delete, collection_name=collection,
                points_selector=selector, wait=True),
            f"Qdrant collection '{collection}' does not exist.")
        return {"deleted": len(ids) if ids else "filter"}

    async def close(self) -> None:
        if self._client is not None:
            try:
                if self._local_key is not None:
                    def release():
                        with _LOCAL_GUARD:
                            item = _LOCAL_CLIENTS[self._local_key]
                            item['refs'] -= 1
                            if item['refs'] == 0:
                                item['client'].close()
                                del _LOCAL_CLIENTS[self._local_key]
                    await self._run(release)
                else:
                    await self._run(self._client.close)
            except Exception:
                pass
            self._client = None
            self._local_key = self._local_lock = None


def create() -> QdrantDriver:
    return QdrantDriver()

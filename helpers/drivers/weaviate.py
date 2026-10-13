"""Weaviate driver: vector collections (v4 client, worker thread)."""

from __future__ import annotations

import asyncio
import time
import uuid
from urllib.parse import urlparse

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "weaviate"


class WeaviateDriver(VectorDriver):
    capabilities: set[str] = {"vector"}

    def __init__(self):
        self._client = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import weaviate
        from weaviate.classes.init import Auth

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        dsn = (resolved_dsn or "").strip()
        if not dsn.startswith("http"):
            raise DbHubError(
                "Weaviate dsn must be an http(s):// URL, e.g. "
                "http://localhost:8080")
        parsed = urlparse(dsn)
        secure = parsed.scheme == "https"
        auth = None
        if self._options.get('oauth_access_token'):
            auth = Auth.bearer_token(self._options['oauth_access_token'], expires_in=self._options.get('oauth_expires_in',3600))
        elif self._options.get("api_key"):
            auth = Auth.api_key(str(self._options["api_key"]))
        grpc_host = str(self._options.get("grpc_host", parsed.hostname or ""))
        grpc_port = int(self._options.get("grpc_port", 50051 if not secure else 443) or 0)
        grpc_secure = bool(self._options.get("grpc_secure", secure))

        def _run():
            return weaviate.connect_to_custom(
                http_host=parsed.hostname or "localhost",
                http_port=parsed.port or (443 if secure else 8080),
                http_secure=secure,
                grpc_host=grpc_host, grpc_port=grpc_port,
                grpc_secure=grpc_secure,
                auth_credentials=auth,
            )

        self._client = await asyncio.to_thread(_run)

    async def test(self) -> dict:
        t0 = time.monotonic()
        ready = await asyncio.to_thread(self._client.is_ready)
        names = await asyncio.to_thread(
            lambda: list(self._client.collections.list_all()))
        return {
            "ok": bool(ready),
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "collections": sorted(names),
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            if target:
                config = self._client.collections.get(target).config.get()
                return {"collection": target,
                        "properties": [p.name for p in config.properties]}
            return {"collections": sorted(
                self._client.collections.list_all()),
                "hint": "Pass target '<collection>' for properties."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        raise DbHubError(
            "Weaviate is vector-only; use db_vector search/upsert/delete.")

    def _filter(self, flt: dict | None):
        if not flt:
            return None
        from weaviate.classes.query import Filter

        conditions = [Filter.by_property(k).equal(v) for k, v in flt.items()
                      if k not in {"sql", "args"}]
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        result = conditions[0]
        for extra in conditions[1:]:
            result = result & extra
        return result

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        def _run():
            coll = self._client.collections.get(collection)
            result = coll.query.near_vector(
                near_vector=[float(x) for x in vector],
                limit=max(1, min(int(top_k or 10), 100)),
                filters=self._filter(flt),
                return_metadata=["distance", "certainty"],
            )
            matches = []
            for obj in result.objects:
                props = dict(obj.properties or {})
                meta = getattr(obj, "metadata", None)
                score = None
                if meta is not None:
                    score = getattr(meta, "certainty", None)
                    if score is None:
                        distance = getattr(meta, "distance", None)
                        score = 1.0 / (1.0 + distance) if distance else None
                matches.append({"id": str(obj.uuid), "_score": score, **props})
            return {"matches": matches}

        return await asyncio.to_thread(_run)

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        vector_name = self._options.get("vector_name")

        def _run():
            from weaviate.classes.data import DataObject

            coll = self._client.collections.get(collection)
            objects = []
            for item in items:
                props = dict(item.get("payload", {}) or {})
                if "text" in item and "text" not in props:
                    props["text"] = item["text"]
                vector = [float(x) for x in item["vector"]]
                objects.append(DataObject(
                    properties=props,
                    uuid=item.get("id") or str(uuid.uuid4()),
                    vector={vector_name: vector} if vector_name else vector))
            response = coll.data.insert_many(objects)
            return len(response.uuids)

        return {"upserted": await asyncio.to_thread(_run)}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        if not ids and not flt:
            raise DbHubError("Weaviate delete needs ids or a filter.")

        def _run():
            from uuid import UUID

            coll = self._client.collections.get(collection)
            if ids:
                for item_id in ids:
                    coll.data.delete_by_id(UUID(str(item_id)))
                return len(ids)
            coll.data.delete_many(where=self._filter(flt))
            return "filter"

        return {"deleted": await asyncio.to_thread(_run)}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
            self._client = None


def create() -> WeaviateDriver:
    return WeaviateDriver()

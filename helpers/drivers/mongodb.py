"""MongoDB driver: document operations plus Atlas Vector Search.

Statement is the collection name; params carry the operation:
{"op": "find", "filter": {...}, "projection": {...}, "limit": n}
{"op": "aggregate", "pipeline": [...]}
{"op": "count", "filter": {...}}
{"op": "insert_one", "doc": {...}} / {"op": "insert_many", "docs": [...]}
{"op": "update_one"/"update_many", "filter": {...}, "update": {...}}
{"op": "delete_one"/"delete_many", "filter": {...}}
"""

from __future__ import annotations

import asyncio
import json
import time

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "mongodb"

READ_OPS = frozenset({
    "find", "find_one", "aggregate", "count", "count_documents",
    "estimated_document_count", "distinct",
})
WRITE_OPS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many",
    "replace_one", "delete_one", "delete_many", "bulk_write",
    "find_one_and_update", "find_one_and_replace", "find_one_and_delete",
    "create_index", "drop_index",
})


def _clean(doc):
    from bson import json_util

    return json.loads(json_util.dumps(doc))


class MongoDriver(VectorDriver):
    capabilities: set[str] = {"api", "vector"}

    def __init__(self):
        self._client = None
        self._db = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import pymongo

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        dsn = (resolved_dsn or "").strip()
        if not dsn.startswith("mongodb"):
            raise DbHubError(
                "MongoDB dsn must be a mongodb:// or mongodb+srv:// URI.")
        auth_options = {}
        if self._options.get('oauth_access_token'):
            from pymongo.auth_oidc import OIDCCallback, OIDCCallbackResult
            token = self._options['oauth_access_token']
            expires = self._options.get('oauth_expires_in',3600)
            class DatabaseOAuthCallback(OIDCCallback):
                def fetch(self, context):
                    return OIDCCallbackResult(access_token=token,expires_in_seconds=expires)
            auth_options = dict(authMechanism='MONGODB-OIDC',authMechanismProperties={'OIDC_CALLBACK':DatabaseOAuthCallback()})
        self._client = await asyncio.to_thread(
            pymongo.MongoClient, dsn,
            **auth_options,
            serverSelectionTimeoutMS=int(
                self._options.get("server_timeout_ms", 10000) or 10000))
        default_db = self._options.get("database", "")
        if default_db:
            self._db = self._client[str(default_db)]

    def _collection(self, name: str):
        if "/" in name or not name:
            raise DbHubError(f"Unsafe collection name '{name}'.")
        if self._db is not None:
            return self._db[name]
        default_db = self._client.get_default_database()
        if default_db is not None:
            return default_db[name]
        raise DbHubError(
            "No database selected: put it in the DSN or options.database.")

    def api_is_write(self, params: dict, options: dict) -> bool:
        op = str((params or {}).get("op", "find")).lower()
        if op in WRITE_OPS:
            return True
        if op == "aggregate" and any(isinstance(stage, dict) and ({"$out", "$merge"} & stage.keys()) for stage in (params or {}).get("pipeline", [])):
            return True
        if op in READ_OPS:
            return False
        return super().api_is_write(params, options)

    async def test(self) -> dict:
        t0 = time.monotonic()
        info = await asyncio.to_thread(
            lambda: self._client.admin.command("ping") or {})
        server = await asyncio.to_thread(
            lambda: self._client.server_info().get("version", ""))
        _ = info
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "version": server,
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        def _run():
            names = self._client.list_database_names()
            out = {}
            for db_name in names:
                if db_name in {"admin", "local", "config"}:
                    continue
                db = self._client[db_name]
                cols = {}
                for col in db.list_collection_names():
                    try:
                        cols[col] = db[col].estimated_document_count()
                    except Exception:
                        cols[col] = None
                out[db_name] = cols
            if target:
                for db_name, cols in out.items():
                    if target in cols:
                        sample = list(self._client[db_name][target].find(
                            {}, limit=1))
                        return {"collection": target, "database": db_name,
                                "sample": _clean(sample[0]) if sample else None}
                return {"collection": target, "matches": []}
            return {"databases": out,
                    "hint": "Pass target '<collection>' for a sample doc."}

        return await asyncio.to_thread(_run)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        collection = (statement or "").strip()
        params = params or {}
        op = str(params.get("op", "find")).lower()
        if op not in READ_OPS | WRITE_OPS:
            raise DbHubError(
                f"Unknown MongoDB op '{op}'. Use find, aggregate, count, "
                "distinct, insert_one/many, update_one/many, delete_one/many.")
        if readonly and self.api_is_write(params, self._options):
            from usr.plugins.db_hub.helpers.base import ReadOnlyError

            raise ReadOnlyError("MongoDB connection is read-only.")

        def _run():
            col = self._collection(collection)
            if op in {"find", "find_one"}:
                cursor = col.find(params.get("filter", {}) or {},
                                  params.get("projection"))
                skip = max(0, int(params.get("skip", 0) or 0))
                if skip: cursor = cursor.skip(skip)
                limit = int(params.get("limit", 0) or 0)
                if limit > 0:
                    cursor = cursor.limit(limit)
                docs = [_clean(d) for d in cursor]
                return {"columns": [], "rows": docs, "rowcount": len(docs)}
            if op == "aggregate":
                docs = [_clean(d) for d in col.aggregate(
                    params.get("pipeline", []) or [])]
                return {"columns": [], "rows": docs, "rowcount": len(docs)}
            if op in {"count", "count_documents"}:
                total = col.count_documents(params.get("filter", {}) or {})
                return {"columns": ["count"], "rows": [{"count": total}],
                        "rowcount": 1}
            if op == "estimated_document_count":
                total = col.estimated_document_count()
                return {"columns": ["count"], "rows": [{"count": total}],
                        "rowcount": 1}
            if op == "distinct":
                values = col.distinct(params.get("field", "_id"),
                                      params.get("filter"))
                return {"columns": ["value"],
                        "rows": [{"value": v} for v in values],
                        "rowcount": len(values)}
            if op == "insert_one":
                result = col.insert_one(params.get("doc", {}) or {})
                return {"columns": [], "rows": [],
                        "rowcount": 1, "inserted_id": str(result.inserted_id)}
            if op == "insert_many":
                result = col.insert_many(params.get("docs", []) or [])
                return {"columns": [], "rows": [],
                        "rowcount": len(result.inserted_ids)}
            if op in {"update_one", "update_many"}:
                fn = col.update_one if op == "update_one" else col.update_many
                result = fn(params.get("filter", {}) or {},
                            params.get("update", {}) or {})
                return {"columns": [], "rows": [],
                        "rowcount": result.modified_count}
            if op in {"delete_one", "delete_many"}:
                fn = col.delete_one if op == "delete_one" else col.delete_many
                result = fn(params.get("filter", {}) or {})
                return {"columns": [], "rows": [],
                        "rowcount": result.deleted_count}
            raise DbHubError(f"MongoDB op '{op}' is not wrapped in v1.")

        return await asyncio.to_thread(_run)

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        index = str(self._options.get("vector_index", "vector_index"))
        path = str(self._options.get("vector_path", "embedding"))
        candidates = int(self._options.get("num_candidates", 150) or 150)
        pipeline: list[dict] = [{
            "$vectorSearch": {
                "index": index, "path": path,
                "queryVector": [float(x) for x in vector],
                "numCandidates": candidates,
                "limit": max(1, min(int(top_k or 10), 100)),
            }
        }]
        if flt:
            pipeline[0]["$vectorSearch"]["filter"] = flt
        pipeline.append({"$project": {"score": {"$meta": "vectorSearchScore"},
                                      "_id": 0, "embedding": 0, path: 0}})
        result = await self.query(collection,
                                  {"op": "aggregate", "pipeline": pipeline},
                                  readonly=True)
        matches = []
        for row in result["rows"]:
            row = dict(row)
            score = row.pop("score", None)
            matches.append({"_score": score, **row})
        return {"matches": matches}

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        docs = []
        for item in items:
            payload = dict(item.get("payload", {}) or {})
            if "text" in item and "text" not in payload:
                payload["text"] = item["text"]
            path = str(self._options.get("vector_path", "embedding"))
            payload[path] = [float(x) for x in item["vector"]]
            if item.get("id") is not None:
                payload["_id"] = item["id"]
            docs.append(payload)
        result = await self.query(collection, {"op": "insert_many",
                                               "docs": docs}, readonly=False)
        return {"upserted": result["rowcount"]}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
            self._client = None
            self._db = None


def create() -> MongoDriver:
    return MongoDriver()

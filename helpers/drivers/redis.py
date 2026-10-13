"""Redis driver: key/value commands plus RediSearch vector search."""

from __future__ import annotations

import asyncio
import re
import struct
import time

from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver

DRIVER_NAME = "redis"

READ_COMMANDS = frozenset({
    "get", "mget", "hget", "hmget", "hgetall", "hkeys", "hvals", "hexists",
    "hlen", "exists", "keys", "scan", "type", "ttl", "pttl", "strlen",
    "llen", "lrange", "lindex", "scard", "smembers", "sismember",
    "zcard", "zrange", "zrevrange", "zscore", "zrank", "pfcount",
    "dbsize", "info", "strlen",
})

WRITE_COMMANDS = frozenset({
    "set", "mset", "setex", "setnx", "getset", "getdel", "del", "unlink",
    "hset", "hmset", "hdel", "hincrby", "expire", "expireat", "pexpire",
    "persist", "rename", "lpush", "rpush", "lpop", "rpop", "lset", "ltrim",
    "sadd", "srem", "zadd", "zrem", "incr", "decr", "incrby", "decrby",
    "append", "setrange", "publish", "flushdb", "flushall",
})

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_:.\-]+$")


class RedisDriver(VectorDriver):
    capabilities: set[str] = {"kv", "vector"}

    def __init__(self):
        self._client = None
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import redis

        self._options = dict(conn_cfg or {})
        if self._client is not None:
            return
        dsn = (resolved_dsn or "").strip()
        if not dsn:
            raise DbHubError("Redis dsn must be a redis:// URL.")
        self._client = await asyncio.to_thread(
            redis.Redis.from_url, dsn,
            socket_timeout=float(self._options.get("timeout_s", 10) or 10))

    def is_read_statement(self, statement: str) -> bool:
        first = (statement or "").strip().split(None, 1)
        word = first[0].lower() if first else ""
        if word in READ_COMMANDS:
            return True
        if word in WRITE_COMMANDS:
            return False
        return False  # unknown Redis commands fail closed

    async def test(self) -> dict:
        t0 = time.monotonic()
        pong = await asyncio.to_thread(self._client.ping)
        info = await asyncio.to_thread(self._client.info, "server")
        return {
            "ok": bool(pong),
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "version": (info or {}).get("redis_version", ""),
            "driver": DRIVER_NAME,
        }

    async def list_schema(self, target: str = "") -> dict:
        dbsize = await asyncio.to_thread(self._client.dbsize)
        modules = []
        try:
            modules = [m.get("name") for m in
                       await asyncio.to_thread(self._client.module_list) or []]
        except Exception:
            pass
        indexes = []
        if "search" in modules:
            try:
                indexes = await asyncio.to_thread(self._client.execute_command, "FT._LIST")
            except Exception:
                pass
        prefix = str(self._options.get("key_prefix", ""))
        sample: list[str] = []
        if target:
            _, sample = await asyncio.to_thread(self._client.scan, 0, match=target, count=50)
            sample = [s.decode() if isinstance(s, bytes) else s for s in sample[:50]]
            return {"pattern": target, "keys": sample}
        if prefix:
            _, sample = await asyncio.to_thread(self._client.scan, 0, match=f"{prefix}*", count=50)
            sample = [s.decode() if isinstance(s, bytes) else s for s in sample[:50]]
        return {"dbsize": dbsize, "modules": modules, "search_indexes": indexes,
                "sample_keys": sample,
                "hint": "Pass target 'prefix:*' to list keys."}

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        import shlex

        if readonly and not self.is_read_statement(statement):
            from usr.plugins.db_hub.helpers.base import ReadOnlyError

            raise ReadOnlyError("Redis connection is read-only.")
        try:
            parts = shlex.split(statement or "", posix=True)
        except ValueError as exc:
            raise DbHubError(f"Could not parse Redis command ({exc}).") from exc
        if not parts:
            raise DbHubError("Redis needs a command, e.g. GET mykey.")
        command, args = parts[0].upper(), parts[1:]
        if command in {"FLUSHDB", "FLUSHALL", "SHUTDOWN", "DEBUG", "CONFIG", "KEYS"}:
            raise DbHubError(f"Redis command {command} is not allowed.")
        raw = await asyncio.to_thread(self._client.execute_command, command, *args)
        rows = self._decode(raw)
        return {"columns": ["result"], "rows": [{"result": rows}], "rowcount": 1}

    def _decode(self, value):
        if isinstance(value, bytes):
            try:
                return value.decode()
            except Exception:
                return repr(value)
        if isinstance(value, (list, tuple)):
            return [self._decode(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return [self._decode(v) for v in sorted(value, key=repr)]
        if isinstance(value, dict):
            return {self._decode(k): self._decode(v) for k, v in value.items()}
        return value

    def _key(self, collection: str, item_id: str) -> str:
        prefix = str(self._options.get("key_prefix", "") or collection)
        key = f"{prefix}:{item_id}"
        if not _SAFE_IDENT_RE.match(key):
            raise DbHubError(f"Unsafe Redis key '{item_id}'.")
        return key

    def _blob(self, vector: list[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *[float(x) for x in vector])

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        index = str(collection or self._options.get("search_index", ""))
        if not index:
            raise DbHubError(
                "Redis vector search needs a RediSearch index name as collection. "
                "Create one with FT.CREATE first.")
        field = str(self._options.get("vector_field", "embedding"))
        k = max(1, min(int(top_k or 10), 100))
        query = f"*=>[KNN {k} @{field} $BLOB]"
        args: list = ["FT.SEARCH", index, query, "PARAMS", "2", "BLOB",
                      self._blob(vector), "SORTBY", "__vector_score",
                      "RETURN", "2", "__vector_score", field, "DIALECT", "2"]
        raw = await asyncio.to_thread(self._client.execute_command, *args)
        matches = []
        items = list(raw[1:]) if isinstance(raw, (list, tuple)) else []
        for i in range(0, len(items) - 1, 2):
            key = items[i]
            fields = items[i + 1] if isinstance(items[i + 1], dict) else {}
            decoded = self._decode(fields)
            if isinstance(decoded, dict):
                decoded.pop(field, None)
                decoded.pop("__vector_score", None)
                score = None
                try:
                    score = float(fields.get(b"__vector_score",
                                             fields.get("__vector_score", 0)))
                except (TypeError, ValueError):
                    score = None
                matches.append({"id": self._decode(key), "_distance": score,
                                **(decoded if isinstance(decoded, dict) else {})})
        return {"matches": matches}

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        field = str(self._options.get("vector_field", "embedding"))
        count = 0
        for item in items:
            key = self._key(collection, str(item.get("id", "")))
            mapping = {field: self._blob(item["vector"])}
            payload = dict(item.get("payload", {}) or {})
            if "text" in item and "text" not in payload:
                payload["text"] = item["text"]
            for name, value in payload.items():
                if isinstance(value, (str, int, float)):
                    mapping[str(name)] = value
            await asyncio.to_thread(self._client.hset, key, mapping=mapping)
            count += 1
        return {"upserted": count}

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        if not ids:
            raise DbHubError("Redis vector delete needs ids.")
        deleted = 0
        for item_id in ids:
            deleted += await asyncio.to_thread(
                self._client.delete, self._key(collection, str(item_id)))
        return {"deleted": deleted}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception:
                pass
            self._client = None


def create() -> RedisDriver:
    return RedisDriver()

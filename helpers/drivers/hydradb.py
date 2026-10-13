"""HydraDB driver: graph DB speaking OpenCypher.

Default transport is Neo4j-compatible Bolt (reuses the Neo4j driver code).
`options.transport: "https"` switches to the HTTPS query API; the endpoint
path and health path are configurable because HydraDB's HTTP surface may vary
between releases.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from usr.plugins.db_hub.helpers.base import (
    DbHubError,
    GraphDriver,
    ReadOnlyError,
    is_write_statement,
)
from usr.plugins.db_hub.helpers.drivers.neo4j import Neo4jDriver

DRIVER_NAME = "hydradb"


class HydraHttpsDriver(GraphDriver):
    capabilities: set[str] = {"cypher"}

    def __init__(self):
        self._client = None
        self._base = ""
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import httpx

        self._options = dict(conn_cfg or {})
        base = str(self._options.get("https_base_url", "") or "").rstrip("/")
        if not base:
            parsed = urlparse(resolved_dsn.strip())
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                port = f":{parsed.port}" if parsed.port else ""
                base = f"{parsed.scheme}://{parsed.hostname}{port}"
        if not base:
            raise DbHubError(
                "HydraDB HTTPS transport needs options.https_base_url "
                "(or an http(s):// dsn)."
            )
        self._base = base
        token = str(self._options.get("auth_token", "") or "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=base, headers=headers,
                timeout=float(self._options.get("timeout_s", 30) or 30),
            )

    def _paths(self) -> tuple[str, str]:
        return (
            str(self._options.get("https_path", "/api/query")),
            str(self._options.get("health_path", "/health")),
        )

    async def test(self) -> dict:
        path, health = self._paths()
        t0 = time.monotonic()
        response = await self._client.get(health)
        ok = 200 <= response.status_code < 300
        return {
            "ok": ok,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "backend": "hydradb",
            "transport": "https",
            "status": response.status_code,
            "query_path": path,
        }

    async def list_schema(self, target: str = "") -> dict:
        path, _ = self._paths()
        payload: dict = {"query": "CALL db.labels()",
                         "params": {}, "target": target or None}
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data
        return {"schema": data}

    async def cypher(self, query_text: str, params: dict, readonly: bool) -> dict:
        if readonly and is_write_statement(query_text):
            raise ReadOnlyError("HydraDB connection is read-only.")
        path, _ = self._paths()
        response = await self._client.post(
            path, json={"query": query_text, "params": params or {},
                        "readonly": readonly})
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "rows" in data:
            return {"columns": data.get("columns", []),
                    "rows": data.get("rows", []),
                    "rowcount": len(data.get("rows", []))}
        return {"columns": [], "rows": data if isinstance(data, list) else [data],
                "rowcount": 1}

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        return await self.cypher(statement, params or {}, readonly)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


class HydraBoltDriver(Neo4jDriver):
    backend = "hydradb"


class HydraDriver(GraphDriver):
    """Transport-dispatching HydraDB driver (Bolt default, HTTPS optional)."""

    capabilities: set[str] = {"cypher"}

    def __init__(self):
        self._inner: GraphDriver = HydraBoltDriver()

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        transport = str((conn_cfg or {}).get("transport", "bolt")).lower()
        self._inner = HydraHttpsDriver() if transport == "https" else HydraBoltDriver()
        await self._inner.connect(conn_cfg, resolved_dsn)

    async def test(self) -> dict:
        return await self._inner.test()

    async def list_schema(self, target: str = "") -> dict:
        return await self._inner.list_schema(target)

    async def cypher(self, query_text: str, params: dict, readonly: bool) -> dict:
        return await self._inner.cypher(query_text, params or {}, readonly)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        return await self.cypher(statement, params or {}, readonly)

    async def close(self) -> None:
        await self._inner.close()


def create() -> HydraDriver:
    return HydraDriver()

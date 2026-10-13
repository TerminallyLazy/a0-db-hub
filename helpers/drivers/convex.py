"""Convex driver: query/mutation/action functions over the HTTPS API."""

from __future__ import annotations

import time

from usr.plugins.db_hub.helpers.base import DbDriver, DbHubError

DRIVER_NAME = "convex"
KINDS = {"query", "mutation", "action"}


class ConvexDriver(DbDriver):
    capabilities: set[str] = {"api"}

    def __init__(self):
        self._client = None
        self._base = ""
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import httpx

        self._options = dict(conn_cfg or {})
        self._base = (resolved_dsn or "").strip().rstrip("/")
        if not self._base.startswith("http"):
            raise DbHubError(
                "Convex dsn must be the deployment URL, e.g. "
                "https://polished-panda-123.convex.cloud"
            )
        if self._client is not None:
            return
        headers = {"Content-Type": "application/json"}
        token = str(self._options.get("auth_token", "") or "")
        if self._options.get('oauth_access_token'):
            headers['Authorization'] = 'Convex ' + self._options['oauth_access_token']
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base, headers=headers,
            timeout=float(self._options.get("timeout_s", 30) or 30),
        )

    def _kind(self, params: dict) -> str:
        kind = str(params.get("kind", "")
                   or self._options.get("default_kind", "query")).lower()
        if kind not in KINDS:
            raise DbHubError(
                f"Unknown Convex kind '{kind}'. Use one of: query, mutation, action."
            )
        return kind

    async def test(self) -> dict:
        t0 = time.monotonic()
        health_function = str(self._options.get("health_function", "") or "")
        if health_function:
            response = await self._client.post(
                "/api/query", json={"path": health_function, "args": {}})
            ok = 200 <= response.status_code < 300
            return {
                "ok": ok, "status": response.status_code,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "deployment": self._base, "health_function": health_function,
            }
        response = await self._client.get("/")
        return {
            "ok": 200 <= response.status_code < 500,
            "status": response.status_code,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "deployment": self._base,
            "note": "Set options.health_function for a real query check.",
        }

    async def list_schema(self, target: str = "") -> dict:
        functions = self._options.get("functions", []) or []
        if target:
            matches = [f for f in functions if target in str(f)]
            return {"function": target, "matches": matches}
        return {
            "functions": functions,
            "note": "Convex has no schema endpoint; list function names in "
                    "options.functions or browse them in the Convex dashboard.",
        }

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        function = (statement or "").strip()
        if not function:
            raise DbHubError("Convex needs a function path as the statement.")
        kind = self._kind(params or {})
        args = (params or {}).get("args", {}) or {}
        if not isinstance(args, dict):
            raise DbHubError("Convex args must be an object.")
        response = await self._client.post(
            f"/api/{kind}", json={"path": function, "args": args})
        if response.status_code >= 400:
            raise DbHubError(
                f"Convex {kind} '{function}' failed "
                f"({response.status_code}): {response.text[:500]}"
            )
        data = response.json()
        result = data.get("value", data) if isinstance(data, dict) else data
        rows = result if isinstance(result, list) else [result]
        return {"columns": [], "rows": rows, "rowcount": len(rows),
                "kind": kind, "function": function}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


def create() -> ConvexDriver:
    return ConvexDriver()

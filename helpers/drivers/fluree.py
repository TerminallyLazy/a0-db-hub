"""Fluree driver: ledger queries over the HTTPS API (no extra install).

Statement selects the ledger (or carries a full JSON query body); params
carry the FlureeQL query map merged over {"ledger": ...}.
"""

from __future__ import annotations

import json
import time

from usr.plugins.db_hub.helpers.base import DbHubError, DbDriver

DRIVER_NAME = "fluree"


class FlureeDriver(DbDriver):
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
                "Fluree dsn must be the server base URL, e.g. "
                "http://localhost:8090")
        if self._client is not None:
            return
        headers = {"Content-Type": "application/json"}
        token = str(self._options.get("auth_token", "") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base, headers=headers,
            timeout=float(self._options.get("timeout_s", 30) or 30),
        )

    def _query_path(self) -> str:
        return str(self._options.get("query_path", "/fluree/query"))

    async def test(self) -> dict:
        t0 = time.monotonic()
        for path in (str(self._options.get("health_path", "/health")), "/"):
            try:
                response = await self._client.get(path)
            except Exception:
                continue
            if response.status_code < 500:
                return {
                    "ok": 200 <= response.status_code < 400,
                    "status": response.status_code,
                    "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                    "deployment": self._base,
                }
        return {"ok": False, "deployment": self._base,
                "error": "Fluree server did not answer."}

    def _body(self, statement: str, params: dict) -> dict:
        text = (statement or "").strip()
        if text.startswith("{"):
            try:
                body = json.loads(text)
                if isinstance(body, dict):
                    body.setdefault("ledger", self._options.get("ledger", ""))
                    return body
            except ValueError:
                pass
        ledger = text or str(self._options.get("ledger", "") or "")
        if not ledger:
            raise DbHubError(
                "Fluree needs a ledger: pass it as the statement or set "
                "options.ledger.")
        body = {"ledger": ledger}
        query = (params or {}).get("query", params) or {}
        if isinstance(query, dict):
            body.update({k: v for k, v in query.items() if k != "ledger"})
        return body

    async def list_schema(self, target: str = "") -> dict:
        ledgers = self._options.get("ledgers", []) or []
        if self._options.get("ledger") and self._options["ledger"] not in ledgers:
            ledgers = [*ledgers, self._options["ledger"]]
        if target:
            return {"ledger": target,
                    "matches": [name for name in ledgers if target in name]}
        return {
            "ledgers": ledgers,
            "note": "Fluree has no fixed schema endpoint; list ledgers in "
                    "options.ledgers or query @context shapes directly.",
        }

    def api_is_write(self, params: dict, options: dict) -> bool:
        return (params or {}).get('kind', 'query') != 'query'

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        kind = (params or {}).get('kind', 'query')
        if kind not in ('query', 'transact'):
            raise DbHubError('Use kind query or transact.')
        if readonly and kind != 'query':
            from usr.plugins.db_hub.helpers.base import ReadOnlyError
            raise ReadOnlyError('Fluree transactions require a writable connection.')
        query = (params or {}).get('query', {k:v for k,v in (params or {}).items() if k != 'kind'})
        body = self._body(statement, {'query': query})
        path = self._query_path() if kind == 'query' else str(self._options.get('transact_path', self._query_path().rsplit('/', 1)[0] + '/transact'))
        response = await self._client.post(path, json=body)
        if response.status_code >= 400:
            raise DbHubError(
                f"Fluree query failed ({response.status_code}): "
                f"{response.text[:500]}"
            )
        data = response.json()
        rows = data if isinstance(data, list) else [data]
        return {"columns": [], "rows": rows, "rowcount": len(rows)}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None


def create() -> FlureeDriver:
    return FlureeDriver()

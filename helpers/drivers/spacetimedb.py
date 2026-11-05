"""SpacetimeDB HTTP SQL and reducer calls; no generated bindings required.

DSN is the server origin; options.database selects the published module.
CALL name is DB Hub syntax for the HTTP reducer endpoint, not Spacetime SQL.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote, urlsplit

from usr.plugins.db_hub.helpers.base import VectorDriver, DbHubError, ReadOnlyError, is_write_statement
from usr.plugins.db_hub.helpers.drivers.spacetimedb_memory import SpacetimeMemory

DRIVER_NAME = "spacetimedb"
_CALL = re.compile(r"CALL\s+([A-Za-z_][A-Za-z0-9_]*)\s*;?", re.IGNORECASE)


def field_name(value, fallback=""):
    if isinstance(value, dict):
        return str(value.get("some", fallback))
    return str(value) if value is not None else fallback


def safe_numbers(value):
    """Retain exact integer values across the browser's JSON number boundary."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 9007199254740991:
        return str(value)
    if isinstance(value, list):
        return [safe_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: safe_numbers(item) for key, item in value.items()}
    return value


def encode_arg(value, typ, types, depth=0):
    """Encode decimal strings as exact JSON integers only in schema integer slots.

    Other values retain SATS-JSON shape. Walk structs, arrays, sums and refs so
    a text field containing digits never silently becomes an integer.
    """
    if depth > 64:
        raise DbHubError("SpacetimeDB argument nesting exceeds 64 levels.")
    if 'Ref' in typ:
        ref = typ['Ref']
        if not isinstance(ref, int) or not 0 <= ref < len(types):
            raise DbHubError("SpacetimeDB reducer contains an invalid type reference.")
        return encode_arg(value, types[ref], types, depth + 1)
    if set(typ) & {'I64', 'U64', 'I128', 'U128'}:
        if isinstance(value, str) and re.fullmatch(r'-?[0-9]+', value):
            return int(value)
    if 'Array' in typ and isinstance(value, list):
        return [encode_arg(item, typ['Array'], types, depth + 1) for item in value]
    if 'Product' in typ:
        fields = typ['Product'].get('elements', [])
        if isinstance(value, list) and len(value) == len(fields):
            return [encode_arg(item, field['algebraic_type'], types, depth + 1) for item, field in zip(value, fields)]
        if isinstance(value, dict):
            fields = {field_name(field.get('name')): field['algebraic_type'] for field in fields}
            return {key: encode_arg(item, fields[key], types, depth + 1) if key in fields else item for key, item in value.items()}
    if 'Sum' in typ and isinstance(value, dict) and len(value) == 1:
        key, item = next(iter(value.items()))
        for i, variant in enumerate(typ['Sum'].get('variants', [])):
            if str(key) in {str(i), field_name(variant.get('name'))}:
                return {key: encode_arg(item, variant['algebraic_type'], types, depth + 1)}
    return value


def server_origin(dsn):
    origin = (dsn or '').strip().rstrip('/')
    try:
        url = urlsplit(origin)
        valid = (url.scheme in {'http', 'https'} and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query
                 and not url.fragment and not url.path)
    except ValueError:
        valid = False
    if not valid:
        raise DbHubError('SpacetimeDB DSN must be a server origin such as https://maincloud.spacetimedb.com or http://localhost:3000. Put the database name in options.database.')
    return origin


class SpacetimeDBDriver(SpacetimeMemory, VectorDriver):
    capabilities = {"sql", "reducers", "vector"}
    required_config_keys = ("database",)

    def __init__(self):
        self._client = None
        self._path = ""
        self._options = {}
        self._memory_path = ""

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        import httpx

        options = conn_cfg or {}
        self._options = dict(options)
        origin = server_origin(resolved_dsn)
        database = options.get("database", "")
        if not isinstance(database, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", database):
            raise DbHubError("SpacetimeDB needs options.database: a published database name or identity.")
        token = options.get("auth_token", "")
        if not isinstance(token, str) or "\n" in token or "\r" in token:
            raise DbHubError("SpacetimeDB options.auth_token must be a Bearer token from Agent Zero Secrets.")
        self._path = "/v1/database/" + quote(database, safe="")
        memory_database = options.get('memory_database', '')
        if memory_database and (not isinstance(memory_database, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,255}', memory_database)):
            raise DbHubError('options.memory_database must be a SpacetimeDB database name or identity.')
        self._memory_path = '/v1/database/' + quote(memory_database, safe='') if memory_database else ''
        if self._client is not None:
            await self.close()
        self._client = httpx.AsyncClient(
            base_url=origin, timeout=30, follow_redirects=False,
            headers={"Authorization": "Bearer " + token} if token else {},
        )

    async def _request(self, method, suffix="", database_path=None, **kwargs):
        import httpx

        if self._client is None:
            raise DbHubError("SpacetimeDB connection is closed.")
        try:
            response = await self._client.request(method, (database_path or self._path) + suffix, **kwargs)
        except httpx.HTTPError as exc:
            # Never echo request headers, URLs or provider bodies (may contain tokens).
            note = " A submitted write may have committed; read back before retrying." if method == "POST" else ""
            raise DbHubError("SpacetimeDB request failed. Check the server and network." + note) from exc
        if not response.is_success:
            guidance = {
                401: "Check options.auth_token and its expiry.",
                403: "The identity lacks permission; private tables and SQL writes may require database ownership.",
                404: "Check options.database and the published module on this server; a private table may be inaccessible to this identity.",
            }.get(response.status_code, "Check the statement, reducer arguments, module permissions and server logs. Private tables may be inaccessible to this identity.")
            raise DbHubError(f"SpacetimeDB returned HTTP {response.status_code}. {guidance}")
        return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            raise DbHubError("SpacetimeDB returned an invalid JSON response.") from exc

    async def test(self) -> dict:
        started = time.monotonic()
        # Verify the configured database AND its schema, not merely host reachability.
        info = self._json(await self._request("GET"))
        await self.list_schema()
        memory = await self.memory_info() if self._memory_path else None
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "database_identity": info.get("database_identity"),
                "memory_ready": memory is not None,
                "note": "Database and schema accessible. Query/reducer permissions depend on the authenticated identity and module."}

    async def _raw_schema(self):
        raw = self._json(await self._request("GET", "/schema", params={"version": "9"}))
        if not isinstance(raw, dict) or not isinstance(raw.get("tables"), list):
            raise DbHubError("SpacetimeDB returned an unsupported schema; expected RawModuleDef version 9.")
        return raw

    async def list_schema(self, target: str = "") -> dict:
        raw = await self._raw_schema()
        types = raw.get("typespace", {}).get("types", [])
        tables = []
        for table in raw["tables"]:
            ref = table.get("product_type_ref")
            product = types[ref].get("Product", {}) if isinstance(ref, int) and 0 <= ref < len(types) else {}
            columns = [{"name": field_name(col.get("name"), str(i)), "type": col.get("algebraic_type", {})}
                       for i, col in enumerate(product.get("elements", []))]
            tables.append({"name": table["name"], "columns": columns,
                           "primary_key": [columns[i]["name"] for i in table.get("primary_key", []) if isinstance(i, int) and 0 <= i < len(columns)],
                           "access": next(iter(table.get("table_access", {})), "Unknown"),
                           "indexes": table.get("indexes", []), "constraints": table.get("constraints", [])})
        reducers = raw.get("reducers", [])
        result = {"tables": tables, "reducers": reducers,
                  "note": "Schema declarations can include private tables that this identity cannot query. Define tables in the module and publish with the SpacetimeDB CLI; use CALL reducer_name with params.args to invoke a reducer."}
        if target:
            result["tables"] = [t for t in tables if t["name"] == target]
            result["reducers"] = [r for r in reducers if r.get("name") == target]
            if not result["tables"] and not result["reducers"]:
                raise DbHubError("No SpacetimeDB table or reducer with that name.")
        return result

    def is_read_statement(self, statement: str) -> bool:
        text = (statement or "").strip()
        # CALL is always a write, including names resembling read-only Neo4j procedures.
        return bool(re.match(r"SELECT\b", text, re.IGNORECASE)) and not is_write_statement(text)

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        statement = (statement or "").strip().rstrip(";").strip()
        if not statement or ";" in statement or len(statement) > 100000:
            raise DbHubError("Run one SpacetimeDB statement at a time without embedded semicolons, up to 100 KB.")
        params = params or {}
        if not isinstance(params, dict):
            raise DbHubError("SpacetimeDB params must be an object.")
        confirmed = params.get("confirmed", True)
        if not isinstance(confirmed, bool):
            raise DbHubError("SpacetimeDB params.confirmed must be true or false.")
        call = _CALL.fullmatch(statement)
        if readonly and not self.is_read_statement(statement):
            raise ReadOnlyError("SpacetimeDB connection is read-only; reducer calls and SQL writes are refused.")
        if call:
            if set(params) - {"args", "confirmed"} or not isinstance(params.get("args", []), list):
                raise DbHubError("SpacetimeDB reducer params use {\"args\": [positional arguments], \"confirmed\": true}.")
            raw = await self._raw_schema()
            reducer = next((r for r in raw.get('reducers', []) if r.get('name') == call[1] and 'some' not in r.get('lifecycle', {})), None)
            if reducer is None:
                raise DbHubError("No callable SpacetimeDB reducer with that name. Inspect db_schema for available reducers.")
            fields = reducer.get('params', {}).get('elements', [])
            args = params.get('args', [])
            if len(args) != len(fields):
                raise DbHubError(f"SpacetimeDB reducer expects {len(fields)} positional arguments; inspect db_schema for their types.")
            args = [encode_arg(value, field['algebraic_type'], raw.get('typespace', {}).get('types', [])) for value, field in zip(args, fields)]
            await self._request("POST", "/call/" + quote(call[1], safe=""),
                                params={"confirmed": str(confirmed).lower()}, json=args)
            # Reducers have no return rows. Never retry a call after ambiguous failure.
            return {"columns": [], "rows": [], "rowcount": 0, "reducer": call[1],
                    "ok": True, "confirmed": confirmed,
                    "note": "Call completed. Read the table to inspect resulting data."}
        if re.match(r"CALL\b", statement, re.IGNORECASE):
            raise DbHubError("Use CALL reducer_name and pass arguments in params.args as a JSON array.")
        if not statement or set(params) - {"confirmed"}:
            raise DbHubError("SpacetimeDB SQL requires a statement and supports no bind parameters. Use SQL literals or CALL reducer_name with params.args.")
        raw = self._json(await self._request("POST", "/sql",
                         params={"confirmed": str(confirmed).lower()}, content=statement.encode(),
                         headers={"Content-Type": "text/plain; charset=utf-8"}))
        if not isinstance(raw, list):
            raise DbHubError("SpacetimeDB returned an unsupported SQL result.")
        if len(raw) != 1:
            raise DbHubError("SpacetimeDB returned multiple results. Submit one statement at a time.")
        result = raw[0]
        columns = [field_name(c.get("name"), str(i)) for i, c in enumerate(result.get("schema", {}).get("elements", []))]
        rows = [dict(zip(columns, safe_numbers(row))) for row in result.get("rows", [])]
        return {"columns": columns, "rows": rows, "rowcount": len(rows), "confirmed": confirmed,
                "stats": result.get('stats', {})}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def create() -> SpacetimeDBDriver:
    return SpacetimeDBDriver()

"""Config normalization for db_hub.

- `normalize_for_save`: strict validation, used by the save hook and API.
- `normalize_for_read`: lenient — fills defaults, never raises on entries,
  and redacts raw credentials when the caller is the settings UI.
"""

from __future__ import annotations

import re

from usr.plugins.db_hub.helpers import secrets_bridge

PLUGIN = "db_hub"
CONN_ID_RE = re.compile(r"^[a-z0-9_]+$")
PREFIX_RE = re.compile(r"^[a-z0-9_]+$")

# Option slots that hold credentials by definition: any non-placeholder
# value in these is refused at save time (a bare token has no detectable
# shape, so the slot name is the signal).
_CREDENTIAL_SLOTS = frozenset({
    "api_key", "apikey", "api-key", "auth_token", "authtoken", "auth-token",
    "password", "passwd", "pwd", "token", "access_token", "secret",
    "oauth_credential", "oauth_access_token", "refresh_token", "client_secret",
})

DEFAULTS = {
    "connections": [],
    "max_rows": 200,
    "query_timeout_s": 30,
    "readonly_default": True,
    "memory_backend": {"enabled": False, "connections": [], "prefix": "mem"},
}


def known_drivers() -> list[str]:
    from usr.plugins.db_hub.helpers import drivers

    return drivers.driver_names()


def _normalize_entry(entry: dict, strict: bool) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("Each connection must be an object.")
    conn_id = str(entry.get("id", "") or "")
    driver = str(entry.get("driver", "") or "")
    if strict:
        if not CONN_ID_RE.match(conn_id):
            raise ValueError(
                f"Connection id '{conn_id}' must match ^[a-z0-9_]+$."
            )
        if driver not in known_drivers():
            raise ValueError(
                f"Unknown driver '{driver}'. Valid drivers: "
                f"{', '.join(known_drivers())}."
            )
        dsn = entry.get("dsn", "")
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError(f"Connection '{conn_id}' needs a non-empty dsn.")
        if strict:
            raw = secrets_bridge.find_raw_credentials(dsn)
            if raw:
                raise ValueError(
                    f"Connection '{conn_id}' keeps a raw password in its DSN, "
                    "which would be saved as plain text. Move it to Agent Zero "
                    "Secrets first — the settings form offers “Move password "
                    "to Secrets” — then reference §§secret(KEY) here."
                )
    options = entry.get("options", {})
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError(f"Connection '{conn_id or '?'}': options must be an object.")
    if strict:
        for name, value in options.items():
            if isinstance(value, str) and secrets_bridge.has_raw_credentials(value):
                raise ValueError(
                    f"Connection '{conn_id or '?'}' keeps a raw credential in "
                    f"options.{name}, which would be saved as plain text. "
                    "Move it to Agent Zero Secrets first, then reference "
                    "§§secret(KEY) here."
                )
            if (isinstance(value, str) and value.strip()
                    and name.lower() in _CREDENTIAL_SLOTS
                    and "§§secret(" not in value):
                raise ValueError(
                    f"Connection '{conn_id or '?'}' keeps a raw credential in "
                    f"options.{name}, which would be saved as plain text. "
                    "Use the “Move to Secrets” button next to the field, "
                    "then reference §§secret(KEY) here."
                )
    readonly = entry.get("readonly", None)
    if readonly is not None and not isinstance(readonly, bool):
        raise ValueError(f"Connection '{conn_id or '?'}': readonly must be true/false/null.")
    description = entry.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValueError(f"Connection '{conn_id or '?'}': description must be text.")
    description = description.strip()[:500]
    last_test = _normalize_last_test(entry.get("last_test"))
    return {
        "id": conn_id,
        "driver": driver,
        "dsn": entry.get("dsn", ""),
        "enabled": bool(entry.get("enabled", True)),
        "readonly": readonly,
        "options": options,
        "description": description,
        "last_test": last_test,
    }


def _normalize_last_test(value) -> dict | None:
    return normalize_last_test(value)


def normalize_last_test(value) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return None
    try:
        latency = float(value.get("latency_ms"))
    except (TypeError, ValueError):
        latency = None
    return {
        "ok": bool(value.get("ok")),
        "latency_ms": latency,
        "at": str(value.get("at", "") or ""),
        "error": str(value.get("error", "") or "")[:500],
    }


def _coerce_int(value, fallback: int, minimum: int, field: str, strict: bool) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"'{field}' must be an integer.")
        return fallback
    if number < minimum:
        if strict:
            raise ValueError(f"'{field}' must be >= {minimum}.")
        return fallback
    return number


def normalize_config(data: dict, strict: bool) -> dict:
    if not isinstance(data, dict):
        raise ValueError("db_hub settings must be an object.")
    raw_conns = data.get("connections", [])
    if raw_conns is None:
        raw_conns = []
    if not isinstance(raw_conns, list):
        raise ValueError("'connections' must be a list.")
    normalized: list[dict] = []
    seen: set[str] = set()
    for entry in raw_conns:
        try:
            item = _normalize_entry(entry, strict=strict)
        except ValueError:
            if strict:
                raise
            continue
        if not item["id"] or not item["driver"]:
            if strict:
                raise ValueError("Each connection needs an 'id' and a 'driver'.")
            continue
        if not strict and not CONN_ID_RE.match(item["id"]):
            continue
        if item["id"] in seen:
            if strict:
                raise ValueError(f"Duplicate connection id '{item['id']}'.")
            continue
        seen.add(item["id"])
        normalized.append(item)
    backend = normalize_memory_backend(
        data.get("memory_backend"), [e["id"] for e in normalized], strict=strict)
    if strict and backend["enabled"]:
        from usr.plugins.db_hub.helpers.drivers import MEMORY_DRIVERS
        for target in backend["connections"]:
            entry = next(e for e in normalized if e["id"] == target["connection"])
            if not entry["enabled"]:
                raise ValueError(f"Memory connection '{entry['id']}' is disabled.")
            if entry["driver"] not in MEMORY_DRIVERS:
                raise ValueError(f"Connection '{entry['id']}' supports database tools but not automatic memory. "
                                 "Choose Postgres/Neon, Qdrant, LanceDB, or ChromaDB.")
            readonly = entry["readonly"]
            if readonly is None:
                readonly = data.get("readonly_default", True)
            if readonly:
                raise ValueError(f"Memory connection '{entry['id']}' must allow writes.")
    return {
        "connections": normalized,
        "max_rows": _coerce_int(data.get("max_rows", 200), 200, 1, "max_rows", strict),
        "query_timeout_s": _coerce_int(
            data.get("query_timeout_s", 30), 30, 1, "query_timeout_s", strict
        ),
        "readonly_default": bool(data.get("readonly_default", True)),
        "memory_backend": backend,
    }


def normalize_memory_backend(value, valid_ids: list[str],
                             strict: bool) -> dict:
    backend = {"enabled": False, "connections": [], "prefix": "mem"}
    if not isinstance(value, dict):
        return backend
    enabled = bool(value.get("enabled", False))
    raw_targets = value.get("connections")
    if raw_targets is None:
        # legacy single-connection shape
        single = str(value.get("connection", "") or "")
        raw_targets = [{"connection": single}] if single else []
    if not isinstance(raw_targets, list):
        if strict:
            raise ValueError("Memory backend connections must be a list.")
        raw_targets = []
    targets: list[dict] = []
    seen: set[str] = set()
    for item in raw_targets:
        if not isinstance(item, dict):
            if strict:
                raise ValueError("Memory backend entries must be objects.")
            continue
        name = str(item.get("connection", "") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        if enabled and name not in valid_ids and strict:
            raise ValueError(
                f"Memory backend connection '{name}' does not exist.")
        targets.append({"connection": name})
    if enabled and not targets and strict:
        raise ValueError(
            "Memory backend is enabled but no connection is selected.")
    prefix = str(value.get("prefix", "") or "mem")
    if not PREFIX_RE.match(prefix):
        if strict:
            raise ValueError(
                "Memory collection prefix must match ^[a-z0-9_]+$.")
        prefix = "mem"
    backend.update(enabled=enabled, connections=targets, prefix=prefix)
    return backend


def normalize_for_save(data: dict) -> dict:
    return normalize_config(data or {}, strict=True)


def normalize_for_read(data: dict | None, hook_context: dict | None = None) -> dict:
    merged = dict(DEFAULTS)
    if isinstance(data, dict):
        merged.update(data)
    normalized = normalize_config(merged, strict=False)
    caller = (hook_context or {}).get("caller", "api")
    if caller == "ui":
        redacted = []
        for entry in normalized["connections"]:
            item = dict(entry)
            item["dsn"] = secrets_bridge.redact_for_ui(item.get("dsn", ""))
            redacted.append(item)
        normalized["connections"] = redacted
    return normalized


def redact_connection(entry: dict, project_name: str = "") -> dict:
    """API-safe view of one connection: never includes resolved secrets."""
    dsn = entry.get("dsn", "")
    last_test = entry.get("last_test")
    redacted_test = None
    if isinstance(last_test, dict):
        error = str(last_test.get("error", "") or "")
        redacted_test = {
            "ok": bool(last_test.get("ok")),
            "latency_ms": last_test.get("latency_ms"),
            "at": last_test.get("at", ""),
            "error": secrets_bridge.mask_text(error, project_name=project_name) if error else "",
        }
    return {
        "id": entry.get("id", ""),
        "driver": entry.get("driver", ""),
        "dsn_preview": secrets_bridge.dsn_preview(dsn),
        "has_secret": secrets_bridge.has_secret_placeholder(dsn),
        "enabled": bool(entry.get("enabled", True)),
        "readonly": entry.get("readonly", None),
        "options": dict(entry.get("options", {}) or {}),
        "description": str(entry.get("description", "") or ""),
        "last_test": redacted_test,
    }

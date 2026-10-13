"""Move a plaintext password out of a DSN and into Agent Zero Secrets.

The user pastes the connection string their provider gave them (password
included); this helper stores the password under a chosen key and returns
the DSN with a `§§secret(KEY)` placeholder. Write path mirrors the fish_audio
precedent: merge-preserving rewrite under the manager lock.
"""

from __future__ import annotations

import io
import os
import re

from dotenv.parser import parse_stream
from helpers import files
from helpers.secrets import DEFAULT_SECRETS_FILE, get_default_secrets_manager

from usr.plugins.db_hub.helpers import secrets_bridge
from usr.plugins.db_hub.helpers.base import DbHubError

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_KEY_LEN = 64


def suggest_key(conn_id: str = "", kind: str = "password") -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", conn_id or "db").strip("_").upper() or "DB"
    suffix = "API_KEY" if kind == "api_key" else "PASS"
    return f"DB_HUB_{base}_{suffix}"[:MAX_KEY_LEN]


def store_secret(key: str, value: str, manager=None) -> str:
    """Validate and write one secret; returns the uppercased key."""
    manager = manager or get_default_secrets_manager()
    name = _validate_new_key(key, manager)
    password = _validate_password(value)
    _write_secret(manager, name, password)
    return name


_USERINFO_SLOT_RE = re.compile(r"://([^/\s:@]+)(?::(.*))?@([^/\s]+)")


def apply_password_slot(dsn: str, key: str) -> str:
    """Point the DSN's userinfo password slot at §§secret(key).

    Replaces a raw password or an existing placeholder, or inserts the slot
    when the userinfo has none. Raises DbHubError when there is no userinfo.
    """
    if not dsn or not isinstance(dsn, str):
        raise DbHubError("No DSN to update.")
    match = _USERINFO_SLOT_RE.search(dsn)
    if not match:
        raise DbHubError(
            "This DSN has no user section (expected scheme://user@host). "
            "Paste the full connection string first."
        )
    user, _, host = match.group(1), match.group(2), match.group(3)
    return (
        dsn[: match.start()]
        + f"://{user}:§§secret({key})@{host}"
        + dsn[match.end():]
    )


def _validate_new_key(key: str, manager) -> str:
    name = (key or "").strip().upper()
    if not KEY_RE.match(name) or len(name) > MAX_KEY_LEN:
        # Never echo the submitted value: it may itself be a pasted secret.
        shown = name if len(name) <= 24 else name[:24] + "…"
        raise DbHubError(
            f"Secret key '{shown}' is invalid. Use letters, numbers, "
            "underscores (start with a letter), max 64 chars. "
            "If you pasted a connection string here by mistake, paste it "
            "into the DSN field instead."
        )
    if os.environ.get(name, "").strip():
        raise DbHubError(
            f"Secret '{name}' is managed by the server environment. "
            "Update it there instead."
        )
    try:
        existing = manager.load_secrets()
    except Exception as exc:
        raise DbHubError(
            "Agent Zero Secrets could not be read. Open Secrets and try again."
        ) from exc
    if name in existing:
        raise DbHubError(
            f"Secret '{name}' already exists. Pick another key name so "
            "nothing gets overwritten."
        )
    return name


def _validate_password(value: str) -> str:
    if not value or len(value) > 2048 or any(ch.isspace() for ch in value):
        raise DbHubError("No extractable password found in this DSN.")
    if "'" in value:
        raise DbHubError(
            "This password contains a quote character; store it through "
            "the Secrets UI instead, then reference §§secret(KEY) here."
        )
    return value


def extract_dsn_secret(dsn: str, key: str = "", conn_id: str = "",
                       manager=None) -> dict:
    """Store the DSN's raw password as `key`, return the rewritten DSN.

    Never returns the password. Raises DbHubError with an actionable message.
    """
    found = secrets_bridge.find_raw_credentials(dsn or "")
    if not found:
        raise DbHubError(
            "No raw password found in this DSN. If the credential is already "
            "a §§secret(KEY) placeholder, nothing needs moving."
        )
    item = next((f for f in found if f["kind"] == "password"), found[0])
    manager = manager or get_default_secrets_manager()
    name = _validate_new_key(key or suggest_key(conn_id, item["key"]), manager)
    password = _validate_password(item["value"])
    _write_secret(manager, name, password)
    rewritten = (dsn or "").replace(item["value"], f"§§secret({name})", 1)
    return {"dsn": rewritten, "key": name}


def _write_secret(manager, name: str, value: str) -> None:
    target_files = list(getattr(manager, "_files", None) or [DEFAULT_SECRETS_FILE])
    if len(target_files) != 1:
        raise DbHubError(
            "Secrets can only be updated when a single secrets file is active."
        )
    try:
        with manager._lock:
            try:
                raw = files.read_file(files.get_abs_path(target_files[0]))
            except Exception:
                raw = ""
            pieces = []
            replaced = False
            for binding in parse_stream(io.StringIO(raw)):
                if getattr(binding, "key", None) == name:
                    if not replaced:
                        pieces.append(f"{name}='{value}'\n")
                    replaced = True
                else:
                    pieces.append(binding.original.string)
            content = "".join(pieces)
            if not replaced:
                content += ("\n" if content and not content.endswith("\n") else "")
                content += f"{name}='{value}'\n"
            manager.save_secrets(content)
    except DbHubError:
        raise
    except Exception as exc:
        raise DbHubError(
            "Could not update Agent Zero Secrets. Check server file permissions."
        ) from exc

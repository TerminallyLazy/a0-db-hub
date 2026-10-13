"""Secrets helpers for db_hub: placeholder resolution and redaction.

All credentials live in the Agent Zero secrets store; plugin config only holds
`§§secret(KEY)` placeholders. Resolved values never leave the driver layer,
never appear in logs, tool output, or API responses.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from helpers.secrets import (
    get_default_secrets_manager,
    get_project_secrets_manager,
    get_secrets_manager,
)

PLACEHOLDER_RE = re.compile(r"§§secret\(([A-Za-z_][A-Za-z0-9_]*)\)")

# Matches password-ish material in raw DSNs: ://user:PASS@ or ;PWD=PASS / password=PASS
_RAW_PASSWORD_RE = re.compile(
    r"(?P<prefix>(://[^/\s:]+:|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]\s*=\s*|pwd\s*=\s*))"
    r"(?P<secret>[^@\s;\"']+)"
)

# userinfo password: scheme://user:PASS@host (PASS may be percent-encoded)
_USERINFO_RE = re.compile(r"://[^/\s:@]+:(?P<pw>[^@/\s]+)@")

# credential-bearing query params: ?password=..&api_key=..&token=..
_PARAM_RE = re.compile(
    r"[?&](?P<key>password|passwd|pwd|api_?key|auth_?token|token)\s*=\s*(?P<val>[^&\s]+)",
    re.IGNORECASE,
)

# Values that are clearly unfilled templates, not real credentials.
_TEMPLATE_HINTS = ("<", ">", "...", "...", "xxx", "changeme", "example", "placeholder")


def _is_template(value: str) -> bool:
    text = unquote(value or "").strip().lower()
    if not text:
        return True
    if text.startswith(("your_", "your-", "<", "[")):
        return True
    return any(hint in text for hint in _TEMPLATE_HINTS)


def find_raw_credentials(dsn: str) -> list[dict]:
    """Find plaintext credentials embedded in a DSN.

    Returns [{kind, key, value}] where value is the raw token exactly as it
    appears (for safe rewriting). Placeholders and template tokens are
    ignored. Empty when nothing secret-like is present.
    """
    found: list[dict] = []
    if not dsn or not isinstance(dsn, str):
        return found
    userinfo = _USERINFO_RE.search(dsn)
    if userinfo:
        password = userinfo.group("pw")
        if "§§secret(" not in password and not _is_template(password):
            found.append({"kind": "password", "key": "password", "value": password})
    for match in _PARAM_RE.finditer(dsn):
        value = match.group("val")
        if "§§secret(" in value or _is_template(value):
            continue
        found.append({"kind": "param", "key": match.group("key").lower(),
                      "value": value})
    return found


def has_raw_credentials(dsn: str) -> bool:
    return bool(find_raw_credentials(dsn))


def manager_for_scope(project_name: str = "", context=None):
    if context is not None:
        return get_secrets_manager(context)
    if project_name:
        return get_project_secrets_manager(project_name, merge_with_global=True)
    return get_default_secrets_manager()


def resolve_placeholders(text: str, project_name: str = "", context=None) -> str:
    """Resolve §§secret(KEY) placeholders; raises RepairableException if missing."""
    if not text or "§§secret(" not in text:
        return text
    return manager_for_scope(project_name, context).replace_placeholders(text)


def mask_text(text: str, project_name: str = "", context=None) -> str:
    if not text:
        return text
    try:
        return manager_for_scope(project_name, context).mask_values(text)
    except Exception:
        return text


def dsn_preview(dsn: str) -> str:
    """Safe display form: placeholders stay literal, raw passwords are masked."""
    if not dsn:
        return ""
    if "§§secret(" in dsn:
        return dsn
    masked = _RAW_PASSWORD_RE.sub(lambda m: m.group("prefix") + "***", dsn)
    if len(masked) > 96:
        return masked[:96] + "…"
    return masked


def has_secret_placeholder(dsn: str) -> bool:
    return bool(dsn) and "§§secret(" in dsn


def redact_for_ui(dsn: str) -> str:
    """Config-hook redaction: raw credentials become a masked preview for display.

    Note: persisting a masked preview would destroy the credential, so the UI
    must keep placeholders (`§§secret(KEY)`) as the editable source of truth.
    """
    if not dsn:
        return ""
    if "§§secret(" in dsn:
        return dsn
    return _RAW_PASSWORD_RE.sub(lambda m: m.group("prefix") + "***", dsn)

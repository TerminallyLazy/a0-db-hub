"""Normalize whatever a provider hands over into a plugin-ready DSN.

Accepts a plain DSN/URL (returned as-is), a `psql` shell command
(`[PGPASSWORD=..] psql -h HOST -p PORT -U USER -d DB`), or a SQLAlchemy URL
(`postgresql+psycopg2://...`, whose dialect suffix asyncpg would reject).
"""

from __future__ import annotations

import re
import shlex
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from usr.plugins.db_hub.helpers.base import DbHubError

_DIALECT_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9.-]*)(\+[^:/]+)?(://.*)$")
_ENV_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?P<body>.*)$", re.DOTALL)
_PSQL_FLAGS = {
    "-h": "host", "--host": "host",
    "-p": "port", "--port": "port",
    "-U": "user", "--username": "user",
    "-d": "dbname", "--dbname": "dbname",
}


def looks_convertible(text: str) -> str | None:
    """Return a format hint, or None for plain/unknown input."""
    stripped = (text or "").strip()
    if not stripped or "§§secret(" in stripped:
        return None
    first = stripped.split(None, 1)[0]
    if first in {"psql"} or stripped.startswith("PGPASSWORD="):
        return "psql"
    if first in {"mongosh"}:
        return "mongosh"
    if first in {"snowsql"} or stripped.startswith("SNOWSQL_"):
        return "snowsql"
    if "psql " in stripped.split("\n", 1)[0] and "-h" in stripped:
        return "psql"
    if "\n" in stripped and "=" in stripped:
        return "env"
    scheme = stripped.split("://", 1)[0].lower()
    if "+" in scheme and "://" in stripped:
        return "sqlalchemy"
    return None


def _strip_dialect(url: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    match = _DIALECT_RE.match(url.strip())
    if not match:
        return url, ["Could not parse this as a URL; pasted as-is."]
    scheme, dialect, rest = match.group(1).lower(), match.group(2), match.group(3)
    if scheme == "postgres":
        scheme = "postgresql"
        notes.append("Normalized scheme postgres:// to postgresql://.")
    if dialect:
        notes.append(f"Removed driver suffix {dialect} (not valid for this driver).")
    return scheme + rest, notes


def _ensure_sslmode(url: str, host: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if "neon.tech" not in (host or ""):
        return url, notes
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query or ""))
    if "sslmode" not in query:
        query["sslmode"] = "require"
        url = urlunparse(parsed._replace(query=urlencode(query)))
        notes.append("Added sslmode=require (required by Neon).")
    return url, notes


_SNOWSQL_FLAGS = {
    "-a": "account", "--accountname": "account",
    "-u": "user", "--username": "user",
    "-w": "warehouse", "--warehouse": "warehouse",
    "-d": "database", "--dbname": "database",
    "-s": "schema", "--schemaname": "schema",
    "-r": "role", "--rolename": "role",
}

# .env keys understood per driver: dsn value keys and secret value keys.
# Secret values are stored under their own (already meaningful) key names.
_ENV_DSN_KEYS = {
    "postgres": ("DATABASE_URL", "POSTGRES_URL", "NEON_DATABASE_URL", "PGURL"),
    "neo4j": ("NEO4J_URI", "NEO4J_URL"),
    "qdrant": ("QDRANT_URL", "QDRANT_URI"),
    "mongodb": ("MONGODB_URI", "MONGO_URL", "MONGODB_URL"),
    "redis": ("REDIS_URL", "UPSTASH_REDIS_URL"),
    "lancedb": ("LANCEDB_URI", "LANCEDB_URL"),
    "milvus": ("MILVUS_URI", "MILVUS_URL", "ZILLIZ_URI"),
    "chromadb": ("CHROMA_URL", "CHROMA_URI", "CHROMA_HOST"),
    "weaviate": ("WEAVIATE_URL", "WEAVIATE_URI"),
    "convex": ("CONVEX_URL",),
    "fluree": ("FLUREE_URL", "FLUREE_URI"),
    "duckdb": ("DUCKDB_PATH", "MOTHERDUCK_DB"),
    "snowflake": ("SNOWFLAKE_URL",),
    "bigquery": (),
    "hydradb": (),
    "sqlalchemy": ("SQLALCHEMY_DATABASE_URL", "DATABASE_URL"),
}
_ENV_SECRET_SLOTS = {
    # env key -> connection option slot
    "qdrant": {"QDRANT_API_KEY": "api_key"},
    "lancedb": {"LANCEDB_API_KEY": "api_key"},
    "milvus": {"MILVUS_TOKEN": "token", "ZILLIZ_TOKEN": "token"},
    "weaviate": {"WEAVIATE_API_KEY": "api_key"},
    "pinecone": {"PINECONE_API_KEY": "api_key"},
    "redis": {"REDIS_PASSWORD": None, "UPSTASH_REDIS_PASSWORD": None},
    "mongodb": {"MONGODB_PASSWORD": None},
    "snowflake": {"SNOWFLAKE_PASSWORD": None, "SNOWSQL_PWD": None},
    "convex": {"CONVEX_AUTH_TOKEN": "auth_token"},
    "fluree": {"FLUREE_AUTH_TOKEN": "auth_token"},
}


def _split_env_lines(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().upper()
        value = value.strip().strip("'\"")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and value:
            pairs[name] = value
    return pairs


def _parse_mongosh(text: str) -> str:
    try:
        parts = shlex.split(text.strip(), posix=True)
    except ValueError as exc:
        raise DbHubError(f"Could not parse this mongosh command ({exc}).") from exc
    if parts and parts[0] == "mongosh":
        parts = parts[1:]
    for token in parts:
        if "://" in token:
            return token.strip("'\"")
    raise DbHubError(
        "No MongoDB connection string found in this mongosh command. "
        "Copy the full command from Atlas (it embeds the URI in quotes).")


def _parse_snowsql(text: str) -> tuple[dict, str | None]:
    try:
        parts = shlex.split(text.strip(), posix=True)
    except ValueError as exc:
        raise DbHubError(f"Could not parse this snowsql command ({exc}).") from exc
    env_password = None
    args: list[str] = []
    for part in parts:
        if "=" in part and not part.startswith("-") and re.match(r"^[A-Za-z_]\w*=", part):
            name, _, value = part.partition("=")
            if name == "SNOWSQL_PWD":
                env_password = value
            continue
        args.append(part)
    if args and args[0] == "snowsql":
        args = args[1:]
    elif "snowsql" in args:
        args = args[args.index("snowsql") + 1:]
    else:
        raise DbHubError("No snowsql command found in this text.")
    fields: dict[str, str] = {}
    skip_next = False
    for i, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in _SNOWSQL_FLAGS:
            if i + 1 >= len(args):
                raise DbHubError(f"snowsql flag {token} has no value.")
            fields[_SNOWSQL_FLAGS[token]] = args[i + 1]
            skip_next = True
            continue
        for flag, slot in _SNOWSQL_FLAGS.items():
            if flag.startswith("--") and token.startswith(flag + "="):
                fields[slot] = token.split("=", 1)[1]
    missing = [k for k in ("account", "user") if not fields.get(k)]
    if missing:
        raise DbHubError(
            "This snowsql command is missing: " + ", ".join(missing) + ".")
    return fields, env_password


def _finalize(dsn: str, conn_id: str, manager, notes: list[str]) -> dict:
    """Move any embedded raw password to Secrets; return the result mapping."""
    from usr.plugins.db_hub.helpers import extract as extract_helper
    from usr.plugins.db_hub.helpers import secrets_bridge

    stored: list[str] = []
    found = secrets_bridge.find_raw_credentials(dsn)
    password_items = [f for f in found if f["kind"] == "password"]
    if password_items:
        key = extract_helper.store_secret(
            extract_helper.suggest_key(conn_id), password_items[0]["value"],
            manager=manager)
        dsn = dsn.replace(password_items[0]["value"], f"§§secret({key})", 1)
        stored.append(key)
        notes.append(f"Moved the password to Secrets as {key}.")
    return {"dsn": dsn, "stored_key": stored[0] if stored else None,
            "stored": stored, "notes": notes}


def _normalize_env(text: str, conn_id: str, driver: str,
                   manager) -> dict | None:
    """Parse a multi-line .env snippet for a known driver. None if no match."""
    from usr.plugins.db_hub.helpers import extract as extract_helper

    pairs = _split_env_lines(text)
    if len(pairs) < 1:
        return None
    notes: list[str] = []
    options: dict = {}
    dsn = ""
    for key in _ENV_DSN_KEYS.get(driver, ()):
        if pairs.get(key):
            dsn = pairs[key].strip().strip("'\"")
            notes.append(f"Used {key} from the pasted snippet.")
            break
    if not dsn:
        for key, value in pairs.items():
            if (key.endswith("_URL") or key.endswith("_URI")) and "://" in value:
                dsn = value
                notes.append(f"Used {key} from the pasted snippet.")
                break
    if not dsn:
        return None
    slots = _ENV_SECRET_SLOTS.get(driver, {})
    stored: list[str] = []
    for key, slot in slots.items():
        if not pairs.get(key) or "§§secret(" in pairs[key]:
            continue
        secret_key = extract_helper.store_secret(key, pairs[key], manager=manager)
        stored.append(secret_key)
        if slot:
            options[slot] = f"§§secret({secret_key})"
        notes.append(f"Stored {key} in Secrets.")
    if driver == "convex" and not dsn.startswith("http"):
        slug = dsn.strip().strip("/")
        if re.fullmatch(r"[a-z0-9][a-z0-9\-]*", slug):
            dsn = f"https://{slug}.convex.cloud"
            notes.append("Expanded the deployment name to its Convex URL.")
    return {"dsn": dsn, "stored_key": stored[0] if stored else None,
            "stored": stored, "options": options, "notes": notes}


def _parse_psql(text: str) -> tuple[dict, str | None]:
    try:
        parts = shlex.split(text.strip(), posix=True)
    except ValueError as exc:
        raise DbHubError(f"Could not parse this psql command ({exc}).") from exc
    env_password = None
    args: list[str] = []
    for part in parts:
        if "=" in part and not part.startswith("-") and re.match(r"^[A-Za-z_]\w*=", part):
            name, _, value = part.partition("=")
            if name == "PGPASSWORD":
                env_password = value
            continue
        args.append(part)
    if args and args[0] == "psql":
        args = args[1:]
    elif "psql" in args:
        args = args[args.index("psql") + 1:]
    else:
        raise DbHubError("No psql command found in this text.")
    fields: dict[str, str] = {}
    skip_next = False
    for i, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in _PSQL_FLAGS:
            if i + 1 >= len(args):
                raise DbHubError(f"psql flag {token} has no value.")
            fields[_PSQL_FLAGS[token]] = args[i + 1]
            skip_next = True
        elif token.startswith("--host="):
            fields["host"] = token.split("=", 1)[1]
        elif token.startswith("--port="):
            fields["port"] = token.split("=", 1)[1]
        elif token.startswith("--username="):
            fields["user"] = token.split("=", 1)[1]
        elif token.startswith("--dbname="):
            fields["dbname"] = token.split("=", 1)[1]
    missing = [k for k in ("host", "user", "dbname") if not fields.get(k)]
    if missing:
        raise DbHubError(
            "This psql command is missing: " + ", ".join(missing) + ". "
            "Copy the full command from Neon, or use “Parameters only”."
        )
    return fields, env_password


def normalize_pasted(text: str, conn_id: str = "", manager=None,
                     driver: str = "") -> dict:
    """Convert pasted provider text into {dsn, stored_key, stored, options, notes}.

    Handles plain DSNs, .env lines and snippets, psql/mongosh/snowsql
    commands, SQLAlchemy URLs, and bare Convex deployment names. Embedded
    passwords are moved to Secrets as part of the explicit convert action.
    """
    from usr.plugins.db_hub.helpers import extract as extract_helper

    kind = looks_convertible(text)
    notes: list[str] = []
    stored_key = None
    options: dict = {}
    stripped = (text or "").strip()
    if kind == "env":
        env_result = _normalize_env(stripped, conn_id, driver, manager)
        if env_result is not None:
            return env_result
    env_match = _ENV_LINE_RE.match(stripped)
    if env_match and "\n" not in stripped and "://" in env_match.group("body"):
        notes.append("Stripped the KEY= prefix from a .env line.")
        text = env_match.group("body").strip().strip("'\"")
        kind = looks_convertible(text)
    if kind == "env" and driver:
        env_result = _normalize_env(text or "", conn_id, driver, manager)
        if env_result is not None:
            return env_result
    if kind == "sqlalchemy" or (kind is None and "://" in (text or "")):
        dsn, dialect_notes = _strip_dialect(text or "")
        notes.extend(dialect_notes)
        host = urlparse(dsn).hostname or ""
        dsn, ssl_notes = _ensure_sslmode(dsn, host)
        notes.extend(ssl_notes)
        if not notes:
            notes.append("Looks like a plain DSN already; kept as-is.")
        return {"dsn": dsn, "stored_key": None, "stored": [],
                "options": {}, "notes": notes}
    if kind == "psql":
        fields, env_password = _parse_psql(text or "")
        port = fields.get("port") or "5432"
        user = fields["user"]
        if env_password:
            stored_key = extract_helper.store_secret(
                extract_helper.suggest_key(conn_id), env_password,
                manager=manager)
            password_part = f"§§secret({stored_key})"
            notes.append(f"Moved the password to Secrets as {stored_key}.")
        else:
            password_part = f"§§secret({extract_helper.suggest_key(conn_id)})"
            notes.append("No password in the command (Neon hides it for -W "
                         "logins); add it via “Move password to Secrets”.")
        dsn = (f"postgresql://{user}:{password_part}"
               f"@{fields['host']}:{port}/{fields['dbname']}")
        dsn, ssl_notes = _ensure_sslmode(dsn, fields["host"])
        notes.extend(ssl_notes)
        if "pooler" in fields["host"] or port == "6543":
            notes.append("Pooler host detected; kept as given.")
        return {"dsn": dsn, "stored_key": stored_key, "stored": [],
                "options": {}, "notes": notes}
    if kind == "mongosh":
        uri = _parse_mongosh(text or "")
        notes.append("Extracted the connection string from the mongosh command.")
        finalized = _finalize(uri, conn_id or "mongo", manager, notes)
        finalized["options"] = {}
        return finalized
    if kind == "snowsql":
        fields, env_password = _parse_snowsql(text or "")
        user = fields["user"]
        if env_password:
            stored_key = extract_helper.store_secret(
                extract_helper.suggest_key(conn_id or "snowflake"),
                env_password, manager=manager)
            password_part = f"§§secret({stored_key})"
            notes.append(f"Moved the password to Secrets as {stored_key}.")
        else:
            password_part = f"§§secret({extract_helper.suggest_key(conn_id or 'snowflake')})"
            notes.append("No password in the command; add it via "
                         "“Move password to Secrets”.")
        path = "/".join(p for p in (
            fields.get("warehouse", ""), fields.get("database", ""),
            fields.get("schema", "")) if p)
        dsn = f"snowflake://{user}:{password_part}@{fields['account']}"
        if path:
            dsn += "/" + path
        if fields.get("role"):
            dsn += f"?role={fields['role']}"
        return {"dsn": dsn, "stored_key": stored_key, "stored": [],
                "options": {}, "notes": notes}
    if driver == "convex" or (kind is None and "://" not in (text or "")):
        slug = (text or "").strip().strip("/")
        if re.fullmatch(r"[a-z0-9][a-z0-9\-]*", slug):
            return {"dsn": f"https://{slug}.convex.cloud", "stored_key": None,
                    "stored": [], "options": {},
                    "notes": ["Expanded the deployment name to its Convex URL."]}
    return {"dsn": text or "", "stored_key": None, "stored": [],
            "options": {},
            "notes": ["Unrecognized format; pasted as-is. A full connection "
                      "string, a psql/mongosh/snowsql command, or a .env "
                      "snippet works best."]}

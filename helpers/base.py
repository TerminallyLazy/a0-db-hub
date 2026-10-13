"""Shared driver abstraction for db_hub.

Import-light on purpose: driver modules are imported lazily, so this module
must stay dependency-free (stdlib only) to keep tool/API load paths cheap.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any


class DbHubError(Exception):
    """User-facing db_hub failure with an actionable message."""


class UnknownConnectionError(DbHubError):
    def __init__(self, conn_id: str, valid_ids: list[str]):
        self.conn_id = conn_id
        self.valid_ids = valid_ids
        known = ", ".join(valid_ids) if valid_ids else "(none configured)"
        super().__init__(
            f"Unknown database connection '{conn_id}'. "
            f"Configured connections: {known}."
        )


class ReadOnlyError(DbHubError):
    pass


class DbDriver(ABC):
    """Uniform interface every database driver implements."""

    capabilities: set[str] = set()
    required_config_keys: tuple[str, ...] = ()
    required_secret_keys: tuple[str, ...] = ()

    @abstractmethod
    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        """Open (or reuse) the underlying client for this connection."""

    @abstractmethod
    async def test(self) -> dict:
        """Return {"ok": bool, "latency_ms": float, ...version info}."""

    @abstractmethod
    async def list_schema(self, target: str = "") -> dict:
        """Return schema info; `target` narrows to one table/collection/label."""

    @abstractmethod
    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        """Run a driver-native statement. Must honor `readonly`."""

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying client. Safe to call repeatedly."""

    def is_read_statement(self, statement: str) -> bool:
        """True when a statement is safe under read-only enforcement.

        Defaults to the shared SQL/Cypher classifier; drivers with their own
        command language (e.g. Redis) override the read set.
        """
        return not is_write_statement(statement or "")

    def api_is_write(self, params: dict, options: dict) -> bool:
        """True when an api-capability call mutates state.

        Defaults to the Convex kind convention (mutation/action are writes);
        document-style drivers override with their own operation mapping.
        """
        kind = str(
            (params or {}).get("kind", "")
            or (options or {}).get("default_kind", "query")
        ).lower()
        return kind in {"mutation", "action"}


class VectorDriver(DbDriver):
    capabilities: set[str] = {"vector"}

    @abstractmethod
    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        ...

    @abstractmethod
    async def vector_upsert(
        self, collection: str, items: list[dict[str, Any]]
    ) -> dict:
        """`items`: [{id?, vector, payload?}]. Returns {"upserted": n}."""

    async def vector_delete(
        self, collection: str, ids: list[str] | None = None, flt: dict | None = None
    ) -> dict:
        raise DbHubError("vector delete is not supported by this driver")


class GraphDriver(DbDriver):
    capabilities: set[str] = {"cypher"}

    @abstractmethod
    async def cypher(self, query_text: str, params: dict, readonly: bool) -> dict:
        ...


_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|drop|create|alter|truncate|grant|revoke|"
    r"merge|replace|copy|vacuum|reindex|cluster|comment|"
    r"detach\s+delete)\b",
    re.IGNORECASE | re.DOTALL,
)

_CYPHER_WRITE_RE = re.compile(
    r"\b(detach\s+delete|delete|remove|set|merge|create|drop)\b",
    re.IGNORECASE,
)

_CALL_WRITE_RE = re.compile(
    r"\b(creat\w*|merg\w*|delet\w*|detach|remov\w*|\bset\b|\bdrop\b|"
    r"add\s+constraint|drop\s+constraint|start\s+transaction|commit)\b",
    re.IGNORECASE,
)

_WITH_WRITE_CTE_RE = re.compile(
    r"\b(insert\s+into|update\s+.+\s+set|delete\s+from|merge\s+into)\b",
    re.IGNORECASE | re.DOTALL,
)

_MUTATING_CLAUSE_RE = re.compile(
    r"\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|"
    r"replace|copy|vacuum|reindex|cluster|comment|set|remove|into|"
    r"attach|detach|install|load|commit|rollback)\b",
    re.IGNORECASE,
)

_READ_PROCEDURES = {
    "db.labels", "db.relationshiptypes", "db.propertykeys",
    "db.schema.nodetypeproperties", "db.schema.reltypeproperties",
    "db.schema.visualization", "db.index.vector.querynodes",
    "db.index.vector.queryrelationships",
}


def is_write_statement(statement: str) -> bool:
    """Conservative read/write classifier for SQL/Cypher text.

    Anything unrecognized is treated as a write (fail closed).
    """
    text = (statement or "").strip().rstrip(";").strip()
    if not text:
        return True
    # A read prefix does not make a batch, executing EXPLAIN, or procedure
    # read-only. Reject ambiguous text before reaching any driver. This is
    # deliberately conservative even for punctuation inside string literals;
    # callers can bind literal values as parameters instead.
    if ";" in text or "--" in text or "/*" in text:
        return True
    if _MUTATING_CLAUSE_RE.search(text):
        return True
    calls = list(re.finditer(r"\bcall\b", text, re.IGNORECASE))
    for call in calls:
        procedure = re.match(r"\s+([a-z0-9_.]+)\s*\(", text[call.end():], re.IGNORECASE)
        if not procedure or procedure[1].lower() not in _READ_PROCEDURES:
            return True
    first = text.split(None, 1)[0].lower().rstrip(";") if text.split() else ""
    if first in {"select", "with", "explain", "show", "describe", "desc", "match",
                 "optional", "return", "unwind", "call"}:
        if _WITH_WRITE_CTE_RE.search(text):
            return True
        if first == "call" and _CALL_WRITE_RE.search(text):
            return True
        if first in {"match", "optional", "unwind"} and _CYPHER_WRITE_RE.search(text):
            return True
        return False
    return True


def truncate_rows(rows: list, max_rows: int) -> tuple[list, bool]:
    if max_rows > 0 and len(rows) > max_rows:
        return rows[:max_rows], True
    return rows, False

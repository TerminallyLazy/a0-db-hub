"""Neo4j driver: OpenCypher over Bolt (async), incl. 5.x vector indexes."""

from __future__ import annotations

import datetime
import time
from urllib.parse import unquote, urlparse

from usr.plugins.db_hub.helpers.base import (
    DbHubError,
    GraphDriver,
    ReadOnlyError,
    VectorDriver,
    is_write_statement,
)

DRIVER_NAME = "neo4j"


def _plain(value):
    from neo4j.graph import Node, Path, Relationship

    if isinstance(value, Node):
        return {"_id": value.element_id, "_labels": list(value.labels), **dict(value)}
    if isinstance(value, Relationship):
        return {"_id": value.element_id, "_type": value.type,
                "_start": value.start_node.element_id,
                "_end": value.end_node.element_id, **dict(value)}
    if isinstance(value, Path):
        return {"_nodes": [_plain(n) for n in value.nodes],
                "_relationships": [_plain(r) for r in value.relationships]}
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _split_dsn(dsn: str) -> tuple[str, tuple | None, str]:
    """Return (bolt uri without credentials, auth tuple|None, database)."""
    parsed = urlparse(dsn.strip())
    if parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j",
                             "neo4j+s", "neo4j+ssc"}:
        raise DbHubError(
            "Neo4j dsn must be a bolt(s):// or neo4j(s):// URI, e.g. "
            "bolt://neo4j:PASS@host:7687/neo4j."
        )
    auth = None
    if parsed.username:
        auth = (unquote(parsed.username),
                unquote(parsed.password or ""))
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    uri = f"{parsed.scheme}://{host}{port}"
    database = (parsed.path or "/").lstrip("/") or "neo4j"
    return uri, auth, database


class Neo4jDriver(GraphDriver, VectorDriver):
    capabilities: set[str] = {"cypher", "vector"}
    backend = "neo4j"

    def __init__(self):
        self._driver = None
        self._database = "neo4j"
        self._options: dict = {}

    async def connect(self, conn_cfg: dict, resolved_dsn: str) -> None:
        from neo4j import AsyncGraphDatabase

        self._options = dict(conn_cfg or {})
        uri, auth, database = _split_dsn(resolved_dsn)
        self._database = str(self._options.get("database", database) or database)
        kwargs: dict = {}
        if self._options.get('oauth_access_token'):
            from neo4j import bearer_auth
            kwargs['auth'] = bearer_auth(self._options['oauth_access_token'])
        elif auth:
            kwargs["auth"] = auth
        if self._driver is None:
            self._driver = AsyncGraphDatabase.driver(uri, **kwargs)

    def _session(self, readonly: bool):
        from neo4j import READ_ACCESS, WRITE_ACCESS

        return self._driver.session(
            database=self._database,
            default_access_mode=READ_ACCESS if readonly else WRITE_ACCESS,
        )

    async def test(self) -> dict:
        t0 = time.monotonic()
        async with self._session(readonly=True) as session:
            record = await session.run("RETURN 1 AS ok")
            await record.single()
            info = await self._driver.get_server_info()
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
            "version": getattr(info, "agent", ""),
            "backend": self.backend,
            "database": self._database,
        }

    async def list_schema(self, target: str = "") -> dict:
        async with self._session(readonly=True) as session:
            if target:
                result = await session.run(
                    "CALL db.schema.nodeTypeProperties() YIELD nodeLabels "
                    "WHERE $label IN nodeLabels "
                    "RETURN nodeLabels, propertyName, propertyTypes LIMIT 200",
                    {"label": target},
                )
                props = [_plain(r.data()) async for r in result]
                return {"label": target, "properties": props}
            labels = await session.run("CALL db.labels() YIELD label RETURN label")
            rels = await session.run(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
            return {
                "labels": [r["label"] async for r in labels],
                "relationship_types": [r["relationshipType"] async for r in rels],
                "hint": "Pass target '<Label>' for property details.",
            }

    async def cypher(self, query_text: str, params: dict, readonly: bool) -> dict:
        if readonly and is_write_statement(query_text):
            raise ReadOnlyError(f"{self.backend} connection is read-only.")
        async with self._session(readonly=readonly) as session:
            async def collect(tx):
                result = await tx.run(query_text, params or {})
                return list(result.keys()), [_plain(r.data()) async for r in result]
            columns, rows = await (session.execute_read(collect) if readonly else session.execute_write(collect))
        return {"columns": columns, "rows": rows, "rowcount": len(rows)}

    async def query(self, statement: str, params: dict, readonly: bool) -> dict:
        return await self.cypher(statement, params or {}, readonly)

    async def vector_search(
        self, collection: str, vector: list[float], top_k: int, flt: dict | None = None
    ) -> dict:
        index = str(collection or self._options.get("vector_index", ""))
        node_label = str(self._options.get("vector_node_label", "Chunk"))
        if not index:
            raise DbHubError(
                "Neo4j vector search needs a collection (index name). "
                "Set options.vector_index or pass the index as collection."
            )
        stmt = (
            "CALL db.index.vector.queryNodes($index, $k, $vector) "
            "YIELD node, score RETURN node, score ORDER BY score DESC"
        )
        result = await self.cypher(
            stmt, {"index": index, "k": max(1, min(int(top_k or 10), 100)),
                   "vector": [float(x) for x in vector]}, readonly=True,
        )
        matches = []
        for row in result["rows"]:
            node = _plain(row.get("node", {}))
            node["_score"] = row.get("score")
            matches.append(node)
        _ = node_label
        return {"matches": matches}

    async def vector_upsert(self, collection: str, items: list[dict]) -> dict:
        raise DbHubError(
            "Neo4j vector upsert is not wrapped in v1; write embeddings with "
            "db_query Cypher (CREATE/MERGE) on a write-enabled connection."
        )

    async def close(self) -> None:
        if self._driver is not None:
            try:
                await self._driver.close()
            except Exception:
                pass
            self._driver = None


def create() -> Neo4jDriver:
    return Neo4jDriver()

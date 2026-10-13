"""db_vector tool: search / upsert / delete on vector-capable connections."""

import json

from helpers.tool import Response, Tool
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers import drivers as driver_registry
from usr.plugins.db_hub.helpers.base import DbHubError, VectorDriver


class DbVector(Tool):
    async def execute(self, connection: str = "", action: str = "search",
                      collection: str = "", query: str = "",
                      documents: list | None = None,
                      vectors: list | None = None,
                      ids: list | None = None,
                      top_k: int = 10, filter: dict | None = None,  # noqa: A002
                      **kwargs) -> Response:
        def mask(text: str) -> str:
            from usr.plugins.db_hub.helpers import secrets_bridge

            return secrets_bridge.mask_text(text, context=self.agent.context)

        action = (action or "search").lower()
        if not connection or not collection:
            return Response(
                message="db_vector needs a connection id and a collection name. "
                "Call db_status first to list configured connections.",
                break_loop=False,
            )
        if action not in {"search", "upsert", "delete"}:
            return Response(
                message="db_vector action must be search, upsert, or delete.",
                break_loop=False)
        if action == "upsert" and not (documents or vectors):
            return Response(
                message="db_vector upsert needs documents or vectors.",
                break_loop=False)
        try:
            bound = await registry.get_connection(connection, agent=self.agent)
            info = driver_registry.get_driver_info(bound.conn_cfg["driver"])
            if "vector" not in info["capabilities"]:
                return Response(
                    message=f"Connection '{connection}' "
                    f"({bound.conn_cfg['driver']}) is not vector-capable.",
                    break_loop=False)
            driver = bound.driver
            if not isinstance(driver, VectorDriver):
                return Response(
                    message=f"Driver '{bound.conn_cfg['driver']}' does not "
                    "implement vector operations.",
                    break_loop=False)
            await self.agent.handle_intervention(
                f"db_hub: vector {action} on '{connection}/{collection}'")
            if action == "search":
                vector = await self._to_vector(vectors, query)
                result = await driver.vector_search(
                    collection, vector, int(top_k or 10), filter or None)
            elif action == "upsert":
                items = await self._to_items(documents, vectors)
                result = await driver.vector_upsert(collection, items)
            else:
                result = await driver.vector_delete(
                    collection, ids=ids, flt=filter or None)
            await registry.evict_idle()
            payload = {"connection": connection, "collection": collection,
                       "action": action, **result}
            return Response(message=mask(json.dumps(payload, default=str)),
                            break_loop=False)
        except DbHubError as exc:
            return Response(message=mask(str(exc)), break_loop=False)
        except Exception as exc:
            return Response(
                message=mask(f"db_vector failed: {type(exc).__name__}: {exc}"),
                break_loop=False)

    async def _to_vector(self, vectors: list | None, query: str) -> list[float]:
        if vectors:
            return [float(x) for x in vectors]
        if not (query or "").strip():
            from usr.plugins.db_hub.helpers.base import DbHubError as _E

            raise _E("db_vector search needs query text or a vectors array.")
        return await self._embed(query)

    async def _to_items(self, documents: list | None,
                        vectors: list | None) -> list[dict]:
        items: list[dict] = []
        for doc in documents or []:
            if not isinstance(doc, dict):
                continue
            item = {"id": doc.get("id"), "payload": {
                k: v for k, v in doc.items() if k not in {"id", "vector", "text"}}}
            if doc.get("text") and not doc.get("vector"):
                item["vector"] = await self._embed(str(doc["text"]))
                item["payload"].setdefault("text", doc["text"])
            elif doc.get("vector"):
                item["vector"] = [float(x) for x in doc["vector"]]
                if doc.get("text"):
                    item["payload"].setdefault("text", doc["text"])
            else:
                continue
            items.append(item)
        for i, vector in enumerate(vectors or []):
            items.append({"id": f"vec-{i}", "vector": [float(x) for x in vector],
                          "payload": {}})
        if not items:
            from usr.plugins.db_hub.helpers.base import DbHubError as _E

            raise _E("Nothing to upsert: give text to embed or explicit vectors.")
        return items

    async def _embed(self, text: str) -> list[float]:
        model = self.agent.get_embedding_model()
        if model is None:
            from usr.plugins.db_hub.helpers.base import DbHubError as _E

            raise _E("No embedding model is configured; cannot embed query text. "
                     "Pass explicit vectors instead.")
        for attr in ("embed_query", "aembed_query"):
            fn = getattr(model, attr, None)
            if callable(fn):
                vector = fn(text)
                if hasattr(vector, "__await__"):
                    vector = await vector
                return [float(x) for x in vector]
        raise DbHubError("The configured embedding model cannot embed text.")

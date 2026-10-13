"""db_query tool: SQL / Cypher / Convex function dispatch."""

import json

from helpers.tool import Response, Tool
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers import drivers as driver_registry
from usr.plugins.db_hub.helpers.base import DbHubError


class DbQuery(Tool):
    async def execute(self, connection: str = "", statement: str = "",
                      params: dict | None = None,
                      readonly: bool | None = None, **kwargs) -> Response:
        def mask(text: str) -> str:
            from usr.plugins.db_hub.helpers import secrets_bridge

            return secrets_bridge.mask_text(text, context=self.agent.context)

        if not connection:
            return Response(
                message="db_query needs a connection id and a statement. "
                "Call db_status first to list configured connections.",
                break_loop=False,
            )
        if not (statement or "").strip():
            return Response(message="db_query needs a non-empty statement.",
                            break_loop=False)
        if params is not None and not isinstance(params, dict):
            return Response(message="db_query params must be an object.",
                            break_loop=False)
        try:
            bound = await registry.get_connection(connection, agent=self.agent)
            info = driver_registry.get_driver_info(bound.conn_cfg["driver"])
            if info["capabilities"].isdisjoint({"sql", "cypher", "api", "kv"}):
                return Response(
                    message=f"Connection '{connection}' ({bound.conn_cfg['driver']}) "
                    "does not run statements; use db_vector for vector drivers.",
                    break_loop=False,
                )
            await self.agent.handle_intervention(
                f"db_hub: querying '{connection}'")
            result = await bound.query(statement, params or {}, readonly)
            await registry.evict_idle()
            payload = {"connection": connection, **result}
            return Response(message=mask(json.dumps(payload, default=str)),
                            break_loop=False)
        except DbHubError as exc:
            return Response(message=mask(str(exc)), break_loop=False)
        except Exception as exc:
            return Response(
                message=mask(f"db_query failed: {type(exc).__name__}: {exc}"),
                break_loop=False)

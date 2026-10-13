"""db_schema tool: introspect tables/collections/labels of one connection."""

import json

from helpers.tool import Response, Tool
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers.base import DbHubError


class DbSchema(Tool):
    async def execute(self, connection: str = "", target: str = "",
                      **kwargs) -> Response:
        def mask(text: str) -> str:
            from usr.plugins.db_hub.helpers import secrets_bridge

            return secrets_bridge.mask_text(text, context=self.agent.context)

        if not connection:
            return Response(
                message="db_schema needs a connection id. "
                "Call db_status first to list configured connections.",
                break_loop=False,
            )
        try:
            await self.agent.handle_intervention(
                f"db_hub: reading schema of '{connection}'")
            bound = await registry.get_connection(connection, agent=self.agent)
            schema = await bound.schema(target or "")
            await registry.evict_idle()
            payload = {"connection": connection, "target": target or None,
                       "schema": schema}
            return Response(message=mask(json.dumps(payload, default=str)),
                            break_loop=False)
        except DbHubError as exc:
            return Response(message=mask(str(exc)), break_loop=False)
        except Exception as exc:
            return Response(
                message=mask(f"db_schema failed: {type(exc).__name__}: {exc}"),
                break_loop=False)

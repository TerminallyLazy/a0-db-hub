"""db_status tool: health-check one or all configured connections."""

import json

from helpers.tool import Response, Tool
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers import drivers as driver_registry
from usr.plugins.db_hub.helpers.base import DbHubError


class DbStatus(Tool):
    async def execute(self, connection: str = "", **kwargs) -> Response:
        def mask(text: str) -> str:
            from usr.plugins.db_hub.helpers import secrets_bridge

            return secrets_bridge.mask_text(text, context=self.agent.context)

        try:
            cfg = registry.load_config(self.agent)
            entries = cfg.get("connections", [])
            if not entries:
                return Response(
                    message="No db_hub connections are configured. "
                    "Add one in Settings → External Services → Database Hub.",
                    break_loop=False,
                )
            targets = [connection] if connection else [
                e["id"] for e in entries if e.get("enabled", True)]
            if connection:
                try:
                    registry.find_entry(cfg, connection)
                except DbHubError as exc:
                    return Response(message=mask(str(exc)), break_loop=False)
            await self.agent.handle_intervention(
                f"db_hub: checking {len(targets)} connection(s)")
            results = []
            for conn_id in targets:
                try:
                    bound = await registry.get_connection(conn_id, agent=self.agent)
                    status = await bound.test()
                    results.append({"connection": conn_id, **status})
                except DbHubError as exc:
                    results.append({"connection": conn_id, "ok": False,
                                    "error": str(exc)})
                except Exception as exc:
                    results.append({"connection": conn_id, "ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"})
            await registry.evict_idle()
            payload = {
                "results": results,
                "drivers": driver_registry.driver_availability(),
            }
            return Response(message=mask(json.dumps(payload, default=str)),
                            break_loop=False)
        except DbHubError as exc:
            return Response(message=mask(str(exc)), break_loop=False)
        except Exception as exc:
            return Response(
                message=mask(f"db_status failed: {type(exc).__name__}: {exc}"),
                break_loop=False)

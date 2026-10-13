"""db_memory_migrate tool: copy built-in FAISS memories into a DB connection.

Reads the current scope's FAISS store (read-only; nothing is deleted) and
re-embeds each text into the target vector-capable connection under
`{prefix}_{area}` collections. Long runs respect pause/intervention.
"""

import json

from helpers.tool import Response, Tool
from usr.plugins.db_hub.helpers import memory_backend, registry
from usr.plugins.db_hub.helpers.base import DbHubError


class DbMemoryMigrate(Tool):
    async def execute(self, connection: str = "", limit: int = 500,
                      areas: str = "main,fragments,solutions", **kwargs) -> Response:
        try:
            area_list = tuple(a.strip().lower() for a in str(areas or "").split(",")
                              if a.strip().lower() in memory_backend.AREAS)
            if not area_list:
                area_list = memory_backend.AREAS
            scope = registry.scope_key(self.agent)
            await self.agent.handle_intervention(
                "db_hub: migrating memories to database")
            result = await memory_backend.migrate_scope(
                project_name=scope[0], agent_profile=scope[1],
                connection=connection or "",
                prefix="", areas=area_list,
                limit=max(int(limit or 500), 1),
                progress=self._progress)
            # default prefix comes from the backend mapping when set
            await registry.evict_idle()
            return Response(message=json.dumps(result), break_loop=False)
        except DbHubError as exc:
            return Response(message=str(exc), break_loop=False)
        except Exception as exc:
            return Response(
                message=f"db_memory_migrate failed: {type(exc).__name__}: {exc}",
                break_loop=False)

    async def _progress(self, moved: int) -> None:
        await self.agent.handle_intervention(f"db_hub: migrated {moved} memories")

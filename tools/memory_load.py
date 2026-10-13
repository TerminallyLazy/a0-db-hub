"""memory_load shadow: routes to the DB backend when configured.

Same tool name, args, and output contract as the bundled _memory tool, so
agents and prompts keep working. With no DB backend enabled, this delegates
to the bundled implementation untouched.
"""

from helpers.tool import Response, Tool

DEFAULT_THRESHOLD = 0.7
DEFAULT_LIMIT = 10


class MemoryLoad(Tool):

    async def execute(self, query="", threshold=DEFAULT_THRESHOLD,
                      limit=DEFAULT_LIMIT, filter="", **kwargs):
        from usr.plugins.db_hub.helpers import memory_backend

        routed = await memory_backend.route_memory_tool(
            self.agent, "load",
            {"query": query, "threshold": threshold, "limit": limit,
             "filter": filter})
        if routed is not None:
            return routed
        from plugins._memory.tools.memory_load import MemoryLoad as Bundled

        tool = Bundled(self.agent, self.name, self.method, self.args,
                       self.message, self.loop_data)
        return await tool.execute(query=query, threshold=threshold,
                                  limit=limit, filter=filter, **kwargs)

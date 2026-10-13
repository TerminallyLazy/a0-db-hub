"""memory_forget shadow: routes to the DB backend when configured.

Same tool name, args, and output contract as the bundled _memory tool, so
agents and prompts keep working. With no DB backend enabled, this delegates
to the bundled implementation untouched.
"""

from helpers.tool import Response, Tool

try:
    from plugins._memory.tools.memory_load import DEFAULT_THRESHOLD
except ImportError:
    DEFAULT_THRESHOLD = 0.7


class MemoryForget(Tool):

    async def execute(self, query="", threshold=DEFAULT_THRESHOLD, filter="",
                      **kwargs):
        from usr.plugins.db_hub.helpers import memory_backend

        routed = await memory_backend.route_memory_tool(
            self.agent, "forget",
            {"query": query, "threshold": threshold, "filter": filter})
        if routed is not None:
            return routed
        from plugins._memory.tools.memory_forget import MemoryForget as Bundled

        tool = Bundled(self.agent, self.name, self.method, self.args,
                       self.message, self.loop_data)
        return await tool.execute(query=query, threshold=threshold,
                                  filter=filter, **kwargs)

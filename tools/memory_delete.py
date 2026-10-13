"""memory_delete shadow: routes to the DB backend when configured.

Same tool name, args, and output contract as the bundled _memory tool, so
agents and prompts keep working. With no DB backend enabled, this delegates
to the bundled implementation untouched.
"""

from helpers.tool import Response, Tool


class MemoryDelete(Tool):

    async def execute(self, ids="", **kwargs):
        from usr.plugins.db_hub.helpers import memory_backend

        routed = await memory_backend.route_memory_tool(
            self.agent, "delete", {"ids": ids})
        if routed is not None:
            return routed
        from plugins._memory.tools.memory_delete import MemoryDelete as Bundled

        tool = Bundled(self.agent, self.name, self.method, self.args,
                       self.message, self.loop_data)
        return await tool.execute(ids=ids, **kwargs)

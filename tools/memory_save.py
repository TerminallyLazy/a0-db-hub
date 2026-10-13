"""memory_save shadow: routes to the DB backend when configured.

Same tool name, args, and output contract as the bundled _memory tool, so
agents and prompts keep working. With no DB backend enabled, this delegates
to the bundled implementation untouched.
"""

from helpers.tool import Response, Tool


class MemorySave(Tool):

    async def execute(self, text="", area="", **kwargs):
        from usr.plugins.db_hub.helpers import memory_backend

        routed = await memory_backend.route_memory_tool(
            self.agent, "save", {"text": text, "area": area, **kwargs})
        if routed is not None:
            return routed
        from plugins._memory.tools.memory_save import MemorySave as Bundled

        tool = Bundled(self.agent, self.name, self.method, self.args,
                       self.message, self.loop_data)
        return await tool.execute(text=text, area=area, **kwargs)

"""Unique hook: still runs when an old framework selects bundled memory hooks."""
from helpers.extension import Extension


class DbHubMemoryGuard(Extension):
    async def execute(self, **kwargs):
        from usr.plugins.db_hub.helpers import compatibility, memory_backend
        if self.agent and memory_backend.backend_enabled(self.agent):
            compatibility.require(self.agent)

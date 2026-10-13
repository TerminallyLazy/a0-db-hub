"""Init shadow: skip FAISS boot when the DB backend owns memory.

The bundled initializer loads the FAISS store and preloads knowledge files
on every monologue. With a database backend enabled that work is wasted, so
this shadow returns early; otherwise it delegates untouched.
"""

import importlib.util
import os

from helpers.extension import Extension
from agent import LoopData


def _bundled_path():
    from helpers.plugins import find_plugin_dir
    from helpers import files

    plugin_dir = find_plugin_dir("_memory")
    if not plugin_dir:
        return None
    path = files.get_abs_path(
        plugin_dir, "extensions", "python", "monologue_start",
        "_10_memory_init.py")
    return path if os.path.exists(path) else None


def _use_backend(agent) -> bool:
    if agent is None:
        return False
    from usr.plugins.db_hub.helpers import memory_backend

    return memory_backend.backend_enabled(agent)


class MemoryInit(Extension):

    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return
        if _use_backend(self.agent):
            from usr.plugins.db_hub.helpers.memory_setup import prepare_for_agent
            await prepare_for_agent(self.agent)
            return
        from helpers import plugins
        if "_memory" not in plugins.get_enabled_plugins(self.agent):
            return
        path = _bundled_path()
        if path is None:
            return None
        spec = importlib.util.spec_from_file_location(
            "dbhub_bundled_memory_init", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tool = module.MemoryInit(self.agent)
        return await tool.execute(loop_data=loop_data, **kwargs)

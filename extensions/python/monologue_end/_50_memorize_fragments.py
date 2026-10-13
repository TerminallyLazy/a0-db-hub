"""Memorizer shadow: background fragment learning into the DB backend.

Mirrors the bundled _memory fragment pipeline (gates, utility-model
summarization, quality filtering, replace-on-similar) but stores fragments
in the configured database instead of FAISS. Intelligent consolidation is
not replicated: near-duplicates above the replace threshold are removed
before insert. With no backend enabled, delegates to bundled code untouched.
"""

import asyncio
import importlib.util
import os

from helpers import errors, plugins
from helpers.extension import Extension
from helpers.dirty_json import DirtyJson
from agent import LoopData
from helpers.log import LogItem
from helpers.defer import DeferredTask, THREAD_BACKGROUND

AREA = "fragments"
SUM_PROMPT = "memory.memories_sum.sys.md"


def _bundled_path():
    from helpers.plugins import find_plugin_dir
    from helpers import files

    plugin_dir = find_plugin_dir("_memory")
    if not plugin_dir:
        return None
    path = files.get_abs_path(
        plugin_dir, "extensions", "python", "monologue_end",
        "_50_memorize_fragments.py")
    return path if os.path.exists(path) else None


def _use_backend(agent) -> bool:
    if agent is None:
        return False
    from usr.plugins.db_hub.helpers import memory_backend

    return memory_backend.backend_enabled(agent)


class MemorizeMemories(Extension):

    def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return
        if not _use_backend(self.agent):
            if "_memory" not in plugins.get_enabled_plugins(self.agent):
                return
            path = _bundled_path()
            if path is None:
                return None
            spec = importlib.util.spec_from_file_location(
                "dbhub_bundled_memorize_fragments", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            tool = module.MemorizeMemories(self.agent)
            return tool.execute(loop_data=loop_data, **kwargs)

        set = plugins.get_plugin_config("_memory", self.agent)
        if not set:
            return None
        if not set["memory_memorize_enabled"]:
            return

        log_item = self.agent.context.log.log(
            type="util",
            heading="Memorizing new information...",
        )
        task = DeferredTask(thread_name=THREAD_BACKGROUND)
        task.start_task(self.memorize, loop_data, log_item)

    async def memorize(self, loop_data: LoopData, log_item: LogItem, **kwargs):
        if not self.agent:
            return
        try:
            set = plugins.get_plugin_config("_memory", self.agent)
            if not set:
                return None
            from usr.plugins.db_hub.helpers import memory_backend
            from plugins._memory.helpers.memory_quality import (
                filter_auto_memory_fragments,
            )

            system = self.agent.read_prompt(SUM_PROMPT)
            msgs_text = self.agent.concat_messages(self.agent.history)
            max_chars = 80000
            if len(msgs_text) > max_chars:
                msgs_text = msgs_text[-max_chars:]
            memories_json = await self.agent.call_utility_model(
                system=system, message=msgs_text, background=True)
            log_item.update(content=memories_json)
            if not memories_json or not isinstance(memories_json, str):
                log_item.update(heading="No response from utility model.")
                return
            memories_json = memories_json.strip()
            if not memories_json:
                log_item.update(heading="Empty response from utility model.")
                return
            try:
                memories = DirtyJson.parse_string(memories_json)
            except Exception as e:
                log_item.update(
                    heading=f"Failed to parse memories response: {str(e)}")
                return
            if memories is None:
                log_item.update(heading="No valid memories found in response.")
                return
            if not isinstance(memories, list):
                if isinstance(memories, (str, dict)):
                    memories = [memories]
                else:
                    log_item.update(heading="Invalid memories format received.")
                    return
            if len(memories) == 0:
                log_item.update(heading="No useful information to memorize.")
                return
            raw_count = len(memories)
            memories = filter_auto_memory_fragments(memories)
            filtered_count = raw_count - len(memories)
            if not memories:
                log_item.update(
                    heading="No durable information to memorize.",
                    filtered_memories_count=filtered_count)
                return
            memories_txt = "\n\n".join(memories).strip()
            replace_threshold = float(
                set.get("memory_memorize_replace_threshold", 0) or 0)
            saved = 0
            for memory in memories:
                txt = f"{memory}"
                old_ids = []
                if replace_threshold > 0:
                    found = await memory_backend.memory_search(
                        self.agent, txt, replace_threshold, 100, (AREA,))
                    old_ids = [item["id"] for item in found]
                await memory_backend.memory_save(
                    self.agent, txt, AREA, {"source": "auto"})
                if old_ids:
                    await memory_backend.memory_delete(self.agent, old_ids)
                saved += 1
            log_item.update(
                result=f"{saved} entries memorized to the database.",
                heading=f"{saved} entries memorized to the database.")
        except Exception as e:
            err = errors.format_error(e)
            self.agent.context.log.log(
                type="warning", heading="Memorize memories extension error",
                content=memory_backend._failure(self.agent, "memorize", [err]))

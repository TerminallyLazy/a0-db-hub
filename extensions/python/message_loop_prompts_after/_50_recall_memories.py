"""Recall shadow: automatic memory recall from the DB backend when enabled.

Mirrors the bundled _memory recall pipeline (gates, intervals, background
search task, prompt rendering, and the result-cache protocol consumed by the
recall-wait extension) but searches the configured database instead of FAISS.
With no backend enabled, delegates to the bundled implementation untouched.
"""

import asyncio
import importlib.util

from helpers import cache, dirty_json, errors, log, plugins
from helpers.extension import Extension
from agent import LoopData

DATA_NAME_TASK = "_recall_memories_task"
DATA_NAME_ITER = "_recall_memories_iter"
DATA_NAME_LOOP = "_recall_memories_loop"
DATA_NAME_RESULT = "_recall_memories_result"
DATA_NAME_RESULT_SCOPE = "_recall_memories_result_scope"
SEARCH_TIMEOUT = 30


def apply_recall_result(loop_data, result):
    extras = loop_data.extras_persistent
    extras.pop("memories", None)
    extras.pop("solutions", None)
    if not isinstance(result, dict):
        return
    for key in ("memories", "solutions"):
        if result.get(key):
            extras[key] = result[key]


def get_recall_scope(agent):
    from usr.plugins.db_hub.helpers.memory_backend import backend_targets, scoped_prefix
    from usr.plugins.db_hub.helpers.registry import scope_key

    targets = backend_targets(agent)
    project, profile = scope_key(agent)
    return (project, profile, scoped_prefix(agent, "mem"),
            ",".join(sorted(t["connection"] + ":" + t["prefix"] for t in targets)))

_BUNDLED_PATH = None


def _bundled_module():
    from helpers.plugins import find_plugin_dir
    from helpers import files
    import os

    plugin_dir = find_plugin_dir("_memory")
    if not plugin_dir:
        return None
    path = files.get_abs_path(
        plugin_dir, "extensions", "python", "message_loop_prompts_after",
        "_50_recall_memories.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(
        "dbhub_bundled_recall_memories", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _use_backend(agent) -> bool:
    if agent is None:
        return False
    from usr.plugins.db_hub.helpers import memory_backend

    return memory_backend.backend_enabled(agent)


class RecallMemories(Extension):

    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return
        if not _use_backend(self.agent):
            if "_memory" not in plugins.get_enabled_plugins(self.agent):
                return
            bundled = _bundled_module()
            if bundled is None:
                return None
            tool = bundled.RecallMemories(self.agent)
            return await tool.execute(loop_data=loop_data, **kwargs)

        set = plugins.get_plugin_config("_memory", self.agent)
        if not set:
            return None
        if not set["memory_recall_enabled"]:
            return None

        recall_scope = get_recall_scope(self.agent)
        cached_result = self.agent.get_data(DATA_NAME_RESULT)
        if cached_result is not None:
            if self.agent.get_data(DATA_NAME_RESULT_SCOPE) == recall_scope:
                apply_recall_result(loop_data, cached_result)
            else:
                cached_result = None
            self.agent.set_data(DATA_NAME_RESULT, None)
            self.agent.set_data(DATA_NAME_RESULT_SCOPE, None)

        if loop_data.iteration % set["memory_recall_interval"] != 0:
            return

        previous_task = self.agent.get_data(DATA_NAME_TASK)
        previous_loop = self.agent.get_data(DATA_NAME_LOOP)
        if previous_task and not previous_task.done():
            if previous_loop is loop_data:
                return
            previous_task.cancel()

        if cached_result is None:
            apply_recall_result(loop_data, {})

        log_item = self.agent.context.log.log(
            type="util",
            heading="Searching memories...",
        )
        task = asyncio.create_task(
            self.search_and_cache(
                loop_data=loop_data, log_item=log_item,
                recall_scope=recall_scope, **kwargs))
        self.agent.set_data(DATA_NAME_TASK, task)
        self.agent.set_data(DATA_NAME_ITER, loop_data.iteration)
        self.agent.set_data(DATA_NAME_LOOP, loop_data)


    async def search_and_cache(self, log_item, loop_data, recall_scope,
                               **kwargs):
        if not self.agent:
            return {}
        try:
            result = await asyncio.wait_for(
                self.search_memories(
                    loop_data=loop_data, log_item=log_item, **kwargs),
                timeout=SEARCH_TIMEOUT,
            )
        except TimeoutError:
            log_item.update(heading="Memory recall timed out")
            result = {}
        except Exception as e:
            self.agent.context.log.log(
                type="warning", heading="Memory recall error",
                content=errors.format_error(e))
            result = {}
        from usr.plugins.db_hub.helpers.memory_backend import backend_for
        from usr.plugins.db_hub.helpers.registry import scope_key

        if self._scope_now() == recall_scope:
            self.agent.set_data(DATA_NAME_RESULT, result)
            self.agent.set_data(DATA_NAME_RESULT_SCOPE, recall_scope)
        else:
            result = {}
            self.agent.set_data(DATA_NAME_RESULT, None)
            self.agent.set_data(DATA_NAME_RESULT_SCOPE, None)
        return result

    def _scope_now(self):
        return get_recall_scope(self.agent)

    async def search_memories(self, log_item, loop_data, **kwargs):
        if not self.agent:
            return {}
        set = plugins.get_plugin_config("_memory", self.agent)
        if not set:
            return {}
        from usr.plugins.db_hub.helpers import memory_backend

        system = self.agent.read_prompt("memory.memories_query.sys.md")
        user_instruction = (
            loop_data.user_message.output_text() if loop_data.user_message else "None"
        )
        history = self.agent.history.output_text()[-set["memory_recall_history_len"]:]
        message = self.agent.read_prompt(
            "memory.memories_query.msg.md", history=history,
            message=user_instruction)

        if set["memory_recall_query_prep"]:
            try:
                query = await self.agent.call_utility_model(
                    system=system, message=message)
                query = query.strip()
                log_item.update(query=query)
            except Exception as e:
                err = errors.format_error(e)
                self.agent.context.log.log(
                    type="warning", heading="Recall memories extension error:",
                    content=memory_backend._failure(self.agent, "memorize", [err]))
                query = ""
            if not query:
                log_item.update(heading="Failed to generate memory query")
                return {}
        else:
            query = user_instruction + "\n\n" + history

        if not query or len(query) <= 3:
            log_item.update(
                query="No relevant memory query generated, skipping search")
            return {}

        vector = await memory_backend._embed(self.agent, query)
        threshold = float(set["memory_recall_similarity_threshold"])
        found_main = await memory_backend.memory_search(
            self.agent, query, threshold,
            int(set["memory_recall_memories_max_search"]),
            ("main", "fragments"), "", _vector=vector)
        found_solutions = await memory_backend.memory_search(
            self.agent, query, threshold,
            int(set["memory_recall_solutions_max_search"]),
            ("solutions",), "", _vector=vector)

        memories = found_main[:set["memory_recall_memories_max_result"]]
        solutions = found_solutions[:set["memory_recall_solutions_max_result"]]
        if not memories and not solutions:
            log_item.update(heading="No memories or solutions found")
            return {}

        if set["memory_recall_post_filter"]:
            combined = memories + solutions
            mems_list = {i: item["doc"].page_content for i, item in enumerate(combined)}
            try:
                chosen = await self.agent.call_utility_model(
                    system=self.agent.read_prompt("memory.memories_filter.sys.md"),
                    message=self.agent.read_prompt(
                        "memory.memories_filter.msg.md", memories=mems_list,
                        history=history, message=user_instruction))
                picks = dirty_json.try_parse(chosen)
                kept_mem, kept_sol = [], []
                if isinstance(picks, list):
                    for idx in picks:
                        if isinstance(idx, int) and 0 <= idx < len(combined):
                            (kept_mem if idx < len(memories)
                             else kept_sol).append(combined[idx])
                memories, solutions = kept_mem, kept_sol
            except Exception as e:
                err = errors.format_error(e)
                self.agent.context.log.log(
                    type="warning", heading="Failed to filter relevant memories",
                    content=memory_backend._failure(self.agent, "memorize", [err]))

        memories_txt = "\n\n".join(
            item["doc"].page_content for item in memories) if memories else ""
        solutions_txt = "\n\n".join(
            item["doc"].page_content for item in solutions) if solutions else ""
        if memories_txt:
            log_item.update(memories=memories_txt)
        if solutions_txt:
            log_item.update(solutions=solutions_txt)
        log_item.update(
            heading=f"{len(memories)} memories and {len(solutions)} relevant solutions found")
        result = {}
        if memories_txt:
            result["memories"] = self.agent.parse_prompt(
                "agent.system.memories.md", memories=memories_txt)
        if solutions_txt:
            result["solutions"] = self.agent.parse_prompt(
                "agent.system.solutions.md", solutions=solutions_txt)
        return result

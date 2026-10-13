"""Memorizer shadow: background solution learning into the DB backend.

Mirrors the bundled _memory solution pipeline (gates, utility-model
summarization, replace-on-similar) but stores solutions in the configured
database instead of FAISS. Intelligent consolidation is not replicated.
With no backend enabled, delegates to bundled code untouched.
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

AREA = "solutions"
SUM_PROMPT = "memory.solutions_sum.sys.md"


def _bundled_path():
    from helpers.plugins import find_plugin_dir
    from helpers import files

    plugin_dir = find_plugin_dir("_memory")
    if not plugin_dir:
        return None
    path = files.get_abs_path(
        plugin_dir, "extensions", "python", "monologue_end",
        "_51_memorize_solutions.py")
    return path if os.path.exists(path) else None


def _use_backend(agent) -> bool:
    if agent is None:
        return False
    from usr.plugins.db_hub.helpers import memory_backend

    return memory_backend.backend_enabled(agent)


class MemorizeSolutions(Extension):

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
                "dbhub_bundled_memorize_solutions", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            tool = module.MemorizeSolutions(self.agent)
            return tool.execute(loop_data=loop_data, **kwargs)

        set = plugins.get_plugin_config("_memory", self.agent)
        if not set:
            return None
        if not set["memory_memorize_enabled"]:
            return

        log_item = self.agent.context.log.log(
            type="util",
            heading="Memorizing succesful solutions...",
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

            system = self.agent.read_prompt(SUM_PROMPT)
            msgs_text = self.agent.concat_messages(self.agent.history)
            max_chars = 80000
            if len(msgs_text) > max_chars:
                msgs_text = msgs_text[-max_chars:]
            solutions_json = await self.agent.call_utility_model(
                system=system, message=msgs_text, background=True)
            log_item.update(content=solutions_json)
            if not solutions_json or not isinstance(solutions_json, str):
                log_item.update(heading="No response from utility model.")
                return
            solutions_json = solutions_json.strip()
            if not solutions_json:
                log_item.update(heading="Empty response from utility model.")
                return
            try:
                solutions = DirtyJson.parse_string(solutions_json)
            except Exception as e:
                log_item.update(
                    heading=f"Failed to parse solutions response: {str(e)}")
                return
            if solutions is None:
                log_item.update(heading="No valid solutions found in response.")
                return
            if not isinstance(solutions, list):
                if isinstance(solutions, (str, dict)):
                    solutions = [solutions]
                else:
                    log_item.update(heading="Invalid solutions format received.")
                    return
            if len(solutions) == 0:
                log_item.update(heading="No successful solutions to memorize.")
                return
            solutions_txt = "\n\n".join(
                [str(solution) for solution in solutions]).strip()
            log_item.update(
                heading=f"{len(solutions)} successful solutions to memorize.",
                solutions=solutions_txt)
            replace_threshold = float(
                set.get("memory_memorize_replace_threshold", 0) or 0)
            saved = 0
            for solution in solutions:
                if isinstance(solution, dict):
                    problem = solution.get("problem", "Unknown problem")
                    solution_text = solution.get("solution", "Unknown solution")
                    txt = f"# Problem\n {problem}\n# Solution\n {solution_text}"
                else:
                    txt = f"# Solution\n {str(solution)}"
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
                result=f"{saved} solutions memorized to the database.",
                heading=f"{saved} solutions memorized to the database.")
        except Exception as e:
            err = errors.format_error(e)
            self.agent.context.log.log(
                type="warning", heading="Memorize solutions extension error",
                content=memory_backend._failure(self.agent, "memorize", [err]))

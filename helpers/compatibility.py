"""Check real asset precedence instead of assuming a version label is sufficient."""
from pathlib import Path
from types import SimpleNamespace

from helpers import plugins, subagents
from usr.plugins.db_hub.helpers.base import DbHubError

REQUIREMENT = (
    "Database memory requires Agent Zero's user-plugin precedence fix (PR #1943; "
    "verified on Agent Zero v2.14). "
    "Update Agent Zero before enabling database memory. Database browsing remains available."
)
TOOLS = ("memory_save", "memory_load", "memory_delete", "memory_forget")
HOOKS = {
    "monologue_start": ("MemoryInit",),
    "message_loop_prompts_after": ("RecallMemories", "RecallWait"),
    "monologue_end": ("MemorizeMemories", "MemorizeSolutions"),
}


def status(agent=None, project_name="", agent_profile=""):
    if not callable(getattr(plugins, "get_plugin_root_priority", None)):
        return {"compatible": False, "message": REQUIREMENT}
    if agent is None:
        agent = SimpleNamespace(config=SimpleNamespace(profile=agent_profile),
            context=SimpleNamespace(get_data=lambda key: project_name if key == "project" else None))
    enabled = plugins.get_enabled_plugins(agent)
    if "_memory" not in enabled:
        return {"compatible": False, "message": "Keep the built-in Memory plugin enabled for recall settings and supporting prompts. DB Hub replaces its storage when database memory is enabled."}
    if "db_hub" not in enabled:
        return {"compatible": False, "message": "Enable Database Hub in this settings scope before enabling database memory."}
    root = Path(plugins.find_plugin_dir("db_hub")).resolve()
    for name in TOOLS:
        paths = subagents.get_paths(agent, "tools", name + ".py")
        if not paths or not Path(paths[0]).resolve().is_relative_to(root):
            return {"compatible": False, "message": f"Another plugin or agent profile overrides {name}. " + REQUIREMENT}
    # Class de-duplication, not just file order, determines the active hooks.
    from helpers import extension
    for point, names in HOOKS.items():
        classes = extension._get_extension_classes(point, agent)
        for name in names:
            cls = next((c for c in classes if c.__name__ == name), None)
            if cls is None or not Path(cls.execute.__code__.co_filename).resolve().is_relative_to(root):
                return {"compatible": False, "message": f"Another plugin or agent profile overrides {name}. " + REQUIREMENT}
    return {"compatible": True, "message": "Agent Zero supports database memory routing."}


def require(agent=None, project_name="", agent_profile=""):
    result = status(agent, project_name, agent_profile)
    if not result["compatible"]:
        raise DbHubError(result["message"])

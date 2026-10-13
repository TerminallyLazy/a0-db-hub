"""Explicit, synthetic write/recall/delete verification of saved memory settings."""
import uuid
from types import SimpleNamespace

from helpers import plugins
from usr.plugins.db_hub.helpers import config, memory_backend as memory, registry
from usr.plugins.db_hub.helpers.base import DbHubError


async def verify_memory(project_name="", agent_profile=""):
    from usr.plugins.db_hub.helpers import compatibility
    compatibility.require(project_name=project_name, agent_profile=agent_profile)
    cfg = config.normalize_for_save(registry.load_config(
        None, project_name=project_name, agent_profile=agent_profile))
    backend = cfg["memory_backend"]
    if not backend["enabled"]:
        raise DbHubError("Save and enable database memory before verifying it.")
    from plugins._model_config.helpers.model_config import get_config, build_model_config
    import models

    scope = (project_name, agent_profile)
    agent = SimpleNamespace(
        config=SimpleNamespace(profile=agent_profile),
        context=SimpleNamespace(get_data=lambda key: project_name if key == "project" else None))
    if "db_hub" not in plugins.get_enabled_plugins(agent):
        raise DbHubError("Enable Database Hub in this scope before verifying memory.")
    from helpers import subagents
    paths = subagents.get_paths(agent, "tools", "memory_save.py")
    if not paths or "/db_hub/" not in paths[0].replace("\\", "/"):
        raise DbHubError("The framework is still selecting another memory tool. "
                         "Database Hub must take precedence before memory can be activated.")
    model_config = build_model_config(
        get_config(project_name=project_name, agent_profile=agent_profile).get("embedding_model", {}),
        models.ModelType.EMBEDDING)
    agent.get_embedding_model = lambda: models.get_embedding_model(
        model_config.provider, model_config.name, model_config=model_config,
        **model_config.build_kwargs())
    text = "DB Hub synthetic memory verification " + uuid.uuid4().hex
    results = []
    for target in backend["connections"]:
        name, prefix = target["connection"], backend["prefix"]
        memory_id = memory._new_id("main")
        result = {"connection": name, "write": False, "recall": False, "delete": False}
        try:
            await memory._save_to_target(agent, memory_id, text, "main",
                                         {"source": "db_hub_verification"}, name, prefix, scope)
            result["write"] = True
            found = await memory.memory_search(agent, text, 0.0, 10, ("main",),
                                                connection=name, prefix=prefix, scope=scope)
            result["recall"] = any(item["id"] == memory_id and item["doc"].page_content == text
                                   for item in found)
            if not result["recall"]:
                raise DbHubError("The saved synthetic memory was not returned by vector search.")
        except Exception as exc:
            result["error"] = memory._failure(agent, "verification", [str(exc)])
        finally:
            try:
                await memory.memory_delete(agent, [memory_id], name, prefix, scope)
                remaining = await memory.memory_search(agent, text, 0.0, 10, ("main",),
                                                        connection=name, prefix=prefix, scope=scope)
                result["delete"] = not any(item["id"] == memory_id for item in remaining)
                if not result["delete"]:
                    raise DbHubError(f"Synthetic memory {memory_id} still exists after deletion.")
            except Exception as exc:
                result["cleanup_error"] = memory._failure(agent, "probe cleanup", [str(exc)])
        result["ok"] = all(result[key] for key in ("write", "recall", "delete")) and not result.get("cleanup_error")
        results.append(result)
    return {"ok": all(item["ok"] for item in results), "results": results}

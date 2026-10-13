"""db_hub settings API: list | save | delete | test | drivers.

Never returns resolved DSNs — only previews with `§§secret(KEY)` placeholders
left literal and raw passwords masked. `test` resolves secrets server-side.
"""

from __future__ import annotations

from helpers import plugins
from helpers.api import ApiHandler, Request, Response
from usr.plugins.db_hub.helpers import config as config_helper
from usr.plugins.db_hub.helpers import registry
from usr.plugins.db_hub.helpers import drivers as driver_registry
from usr.plugins.db_hub.helpers.base import DbHubError

PLUGIN = "db_hub"


def _deps_present() -> bool:
    from helpers import files
    from usr.plugins.db_hub.helpers import bootstrap

    try:
        return bool(files.exists(bootstrap.deps_dir()))
    except Exception:
        return False


async def _test_entry(entry: dict, project_name: str, agent_profile: str) -> dict:
    bound = await registry.get_connection(
        entry.get("id", ""),
        agent=None,
        project_name=project_name,
        agent_profile=agent_profile,
        entry_override=entry,
        pool=False,
    )
    try:
        status = await bound.test()
        return {"connection": entry.get("id", ""), **status}
    finally:
        try:
            await bound.driver.close()
        except Exception:
            pass


class Connections(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        action = str(input.get("action", "list") or "list").lower()
        project_name = str(input.get("project_name", "") or "")
        agent_profile = str(input.get("agent_profile", "") or "")
        try:
            if action == "verify_memory":
                from usr.plugins.db_hub.helpers.verification import verify_memory
                result = await verify_memory(project_name, agent_profile)
                if not result["ok"]:
                    result["error"] = "; ".join(
                        item["connection"] + ": " + (item.get("error") or item.get("cleanup_error") or "verification failed")
                        for item in result["results"] if not item["ok"])
                return result
            if action == "drivers":
                from usr.plugins.db_hub.helpers import compatibility
                return {"drivers": driver_registry.driver_availability(),
                        "memory_compatibility": compatibility.status(project_name=project_name, agent_profile=agent_profile),
                        "deps_present": _deps_present()}
            if action == "install":
                import asyncio

                from usr.plugins.db_hub.helpers import runtime as runtime_helper

                try:
                    manifest = await asyncio.to_thread(
                        runtime_helper.install_dependencies)
                except Exception as exc:
                    return {"ok": False,
                            "error": f"Driver install failed: {exc}"}
                await registry.invalidate_all()
                return {"ok": True,
                        "drivers": driver_registry.driver_availability(),
                        "deps_present": _deps_present(),
                        "installed": (manifest or {}).get("packages", {})}
            if action == "normalize":
                from usr.plugins.db_hub.helpers import importer as importer_helper

                try:
                    result = importer_helper.normalize_pasted(
                        str(input.get("text", "") or ""),
                        conn_id=str(input.get("conn_id", "") or ""),
                        driver=str(input.get("driver", "") or ""),
                    )
                except DbHubError as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "dsn": result["dsn"],
                        "stored_key": result.get("stored_key"),
                        "options": result.get("options", {}),
                        "notes": result.get("notes", [])}
            if action == "store_secret":
                from usr.plugins.db_hub.helpers import extract as extract_helper

                dsn = str(input.get("dsn", "") or "")
                password = str(input.get("password", "") or "")
                if not password:
                    return {"ok": False, "error": "Enter the new password first."}
                try:
                    key = extract_helper.store_secret(
                        str(input.get("key", "") or "") or
                        extract_helper.suggest_key(
                            str(input.get("conn_id", "") or "")),
                        password)
                    if dsn:
                        rewritten = extract_helper.apply_password_slot(dsn, key)
                    else:
                        rewritten = ""
                except DbHubError as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "dsn": rewritten, "key": key}
            if action == "stamp":
                conn_id = str(input.get("id", "") or "")
                stamped = config_helper.normalize_last_test(
                    input.get("last_test") or {})
                if not conn_id or stamped is None:
                    return {"ok": False, "error": "Invalid test result."}
                try:
                    cfg = registry.load_config(
                        None, project_name=project_name,
                        agent_profile=agent_profile)
                    entry = registry.find_entry(cfg, conn_id)
                except DbHubError as exc:
                    return {"ok": False, "error": str(exc)}
                entry["last_test"] = stamped
                try:
                    plugins.save_plugin_config(
                        PLUGIN, project_name=project_name,
                        agent_profile=agent_profile,
                        settings=config_helper.normalize_for_save(cfg))
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
                await registry.invalidate_all()
                return {"ok": True}
            if action == "extract_secret":
                from usr.plugins.db_hub.helpers import extract as extract_helper

                dsn = str(input.get("dsn", "") or "")
                try:
                    result = extract_helper.extract_dsn_secret(
                        dsn,
                        key=str(input.get("key", "") or ""),
                        conn_id=str(input.get("conn_id", "") or ""),
                    )
                except DbHubError as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "dsn": result["dsn"], "key": result["key"]}
            if action == "list":
                cfg = registry.load_config(
                    None, project_name=project_name, agent_profile=agent_profile)
                return {
                    "connections": [config_helper.redact_connection(e, project_name)
                                    for e in cfg.get("connections", [])],
                    "max_rows": cfg.get("max_rows", 200),
                    "query_timeout_s": cfg.get("query_timeout_s", 30),
                    "readonly_default": cfg.get("readonly_default", True),
                    "memory_backend": cfg["memory_backend"],
                }
            if action == "test":
                entry = input.get("connection") or {}
                if isinstance(entry, str):
                    cfg = registry.load_config(
                        None, project_name=project_name,
                        agent_profile=agent_profile)
                    entry = registry.find_entry(cfg, entry)
                normalized = config_helper.normalize_for_save(
                    {"connections": [entry]})
                result = await _test_entry(
                    normalized["connections"][0], project_name, agent_profile)
                return {"ok": bool(result.get("ok")), "result": result}
            if action == "save":
                connections = input.get("connections", [])
                current = registry.load_config(
                    None, project_name=project_name, agent_profile=agent_profile)
                settings = {
                    "connections": connections,
                    "max_rows": input.get("max_rows", 200),
                    "query_timeout_s": input.get("query_timeout_s", 30),
                    "readonly_default": input.get("readonly_default", True),
                    "memory_backend": input.get("memory_backend", current["memory_backend"]),
                }
                normalized = config_helper.normalize_for_save(settings)
                plugins.save_plugin_config(
                    PLUGIN, project_name=project_name,
                    agent_profile=agent_profile, settings=normalized)
                await registry.invalidate_all()
                return {
                    "ok": True,
                    "connections": [config_helper.redact_connection(e, project_name)
                                    for e in normalized["connections"]],
                    "memory_backend": normalized["memory_backend"],
                }
            if action == "delete":
                conn_id = str(input.get("id", "") or "")
                cfg = registry.load_config(
                    None, project_name=project_name, agent_profile=agent_profile)
                kept = [e for e in cfg.get("connections", [])
                        if e.get("id") != conn_id]
                if len(kept) == len(cfg.get("connections", [])):
                    return {"ok": False,
                            "error": f"Unknown connection '{conn_id}'."}
                cfg["connections"] = kept
                plugins.save_plugin_config(
                    PLUGIN, project_name=project_name,
                    agent_profile=agent_profile,
                    settings=config_helper.normalize_for_save(cfg))
                await registry.invalidate_all()
                return {"ok": True}
            return {"ok": False,
                    "error": f"Unknown action '{action}'. "
                    "Use list, save, delete, test, install, extract_secret, store_secret, normalize, stamp, or drivers."}
        except DbHubError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}

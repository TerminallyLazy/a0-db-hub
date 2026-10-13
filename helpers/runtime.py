"""Lifecycle implementation for db_hub hooks (framework runtime).

All dependency state lives in the plugin-owned `deps/` directory so uninstall
removes exactly what this plugin added. Never touches shared packages or the
secrets store.
"""

from __future__ import annotations

import asyncio
import shutil

from usr.plugins.db_hub.helpers import bootstrap, config


def install_dependencies() -> dict:
    bootstrap.pip_install(bootstrap.BASE_DEPS)
    return bootstrap.record_manifest(bootstrap.BASE_DEPS)


def _run(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return loop.create_task(coro)


def close_all_pools_sync() -> None:
    from usr.plugins.db_hub.helpers import registry

    _run(registry.close_detached_pools(registry.detach_all_pools()))


def invalidate_all_pools_sync() -> None:
    from usr.plugins.db_hub.helpers import registry

    _run(registry.close_detached_pools(registry.detach_all_pools()))


def remove_owned_state() -> None:
    from helpers import files

    close_all_pools_sync()
    deps = bootstrap.deps_dir()
    if files.exists(deps):
        shutil.rmtree(deps, ignore_errors=True)
    from pathlib import Path
    previous = Path(deps).parent / '.deps-previous'
    if previous.exists():
        shutil.rmtree(previous)


def normalize_for_read(data: dict, hook_context: dict | None = None) -> dict:
    return config.normalize_for_read(data, hook_context=hook_context)


def normalize_for_save(data: dict) -> dict:
    return config.normalize_for_save(data)

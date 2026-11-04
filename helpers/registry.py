"""Scoped connection pool and dispatch for db_hub.

Pool key is (project_name, agent_profile, conn_id) so the same connection id
can resolve to different DSNs per project/profile without collisions.
DSN `§§secret(KEY)` placeholders resolve at connect time only.
"""

from __future__ import annotations

import asyncio
import json
import time
import threading

from helpers import plugins

from usr.plugins.db_hub.helpers import config as config_helper
from usr.plugins.db_hub.helpers import secrets_bridge
from usr.plugins.db_hub.helpers.base import (
    DbHubError,
    ReadOnlyError,
    UnknownConnectionError,
    is_write_statement,
    truncate_rows,
)

PLUGIN = "db_hub"
IDLE_EVICT_S = 600.0

_POOLS: dict[tuple, "_Pooled"] = {}
_POOL_GUARD = threading.RLock()


class _Pooled:
    def __init__(self):
        from usr.plugins.db_hub.helpers.base import DbDriver

        self.driver: DbDriver | None = None
        self.lock = asyncio.Lock()
        self.last_used = time.monotonic()
        self.fingerprint = ""
        self.loop = asyncio.get_running_loop()


def scope_key(agent=None) -> tuple[str, str]:
    if agent is None:
        return ("", "")
    project = ""
    try:
        from helpers import projects

        project = projects.get_context_project_name(agent.context) or ""
    except Exception:
        project = ""
    profile = ""
    try:
        profile = getattr(getattr(agent, "config", None), "profile", "") or ""
    except Exception:
        profile = ""
    return (project, profile)


def load_config(agent=None, project_name: str = "", agent_profile: str = "") -> dict:
    if agent is not None:
        data = plugins.get_plugin_config(PLUGIN, agent=agent) or {}
    else:
        data = (
            plugins.get_plugin_config(
                PLUGIN, project_name=project_name, agent_profile=agent_profile
            )
            or {}
        )
    return config_helper.normalize_for_read(data)


def find_entry(cfg: dict, conn_id: str) -> dict:
    for entry in cfg.get("connections", []):
        if entry.get("id") == conn_id:
            return entry
    raise UnknownConnectionError(
        conn_id, [e.get("id", "") for e in cfg.get("connections", [])]
    )


def effective_readonly(conn_cfg: dict, glob: dict, explicit: bool | None) -> bool:
    saved = conn_cfg.get('readonly')
    saved = bool(glob.get('readonly_default', True)) if saved is None else bool(saved)
    # A tool may request stricter access, never override saved read-only access.
    return saved or explicit is True


def _fingerprint(conn_cfg: dict) -> str:
    return json.dumps(
        {"dsn": conn_cfg.get("dsn", ""), "options": conn_cfg.get("options", {}),
         "driver": conn_cfg.get("driver", "")},
        sort_keys=True,
        default=str,
    )


def _resolve_options(options: dict, scope: tuple[str, str], context) -> dict:
    """Resolve §§secret(KEY) placeholders inside string option values."""
    resolved = {}
    for key, value in (options or {}).items():
        if isinstance(value, str) and "§§secret(" in value:
            resolved[key] = secrets_bridge.resolve_placeholders(
                value, project_name=scope[0], context=context
            )
        else:
            resolved[key] = value
    return resolved


class BoundConnection:
    """A pooled driver plus the config needed to call it safely."""

    def __init__(self, driver, conn_cfg: dict, glob: dict, scope: tuple[str, str],
                 context=None, sensitive_values=()):
        self.driver = driver
        self.conn_cfg = conn_cfg
        self.glob = glob
        self.scope = scope
        self.context = context
        self.sensitive_values = tuple(v for v in sensitive_values if v)

    def _timeout(self) -> float:
        try:
            return max(1.0, float(self.glob.get("query_timeout_s", 30)))
        except (TypeError, ValueError):
            return 30.0

    def mask(self, text: str) -> str:
        for value in self.sensitive_values:
            text = text.replace(value, "***")
        return secrets_bridge.mask_text(
            text, project_name=self.scope[0], context=self.context
        )

    async def test(self) -> dict:
        await self._touch()
        result = await asyncio.wait_for(self.driver.test(), timeout=self._timeout())
        return result

    async def schema(self, target: str = "") -> dict:
        await self._touch()
        result = await asyncio.wait_for(
            self.driver.list_schema(target or ""), timeout=self._timeout()
        )
        return result

    async def query(
        self, statement: str, params: dict | None = None, readonly: bool | None = None
    ) -> dict:
        from usr.plugins.db_hub.helpers import drivers as registry

        info = registry.get_driver_info(self.conn_cfg["driver"])
        ro = effective_readonly(self.conn_cfg, self.glob, readonly)
        params = params or {}
        options = self.conn_cfg.get("options", {}) or {}
        if ro and "api" in info["capabilities"]:
            if self.driver.api_is_write(params, options):
                raise ReadOnlyError(
                    f"Connection '{self.conn_cfg['id']}' is read-only; "
                    "this call would mutate data."
                )
        elif ro and not self.driver.is_read_statement(statement):
            raise ReadOnlyError(
                f"Connection '{self.conn_cfg['id']}' is read-only; "
                "write statements are refused."
            )
        await self._touch()
        try:
            result = await asyncio.wait_for(
                self.driver.query(statement, params, ro), timeout=self._timeout()
            )
        except asyncio.TimeoutError:
            raise DbHubError(
                "Database query timed out. A submitted write may have committed; "
                "read back before retrying."
            ) from None
        rows = result.get("rows", [])
        if isinstance(rows, list):
            rows, truncated = truncate_rows(rows, int(self.glob.get("max_rows", 200)))
            result["rows"] = rows
            result["truncated"] = truncated or result.get("truncated", False)
        return result

    async def _touch(self) -> None:
        key = (self.scope[0], self.scope[1], self.conn_cfg["id"], asyncio.get_running_loop())
        pooled = _POOLS.get(key)
        if pooled is not None:
            pooled.last_used = time.monotonic()


async def get_connection(
    conn_id: str,
    agent=None,
    project_name: str = "",
    agent_profile: str = "",
    entry_override: dict | None = None,
    pool: bool = True,
) -> BoundConnection:
    """Resolve, connect (lazily), and return a bound connection handle."""
    from usr.plugins.db_hub.helpers import drivers as registry

    scope = scope_key(agent)
    if agent is None and (project_name or agent_profile):
        scope = (project_name, agent_profile)
    context = getattr(agent, "context", None) if agent is not None else None

    if entry_override is not None:
        cfg = config_helper.normalize_for_save(
            {"connections": [entry_override], "max_rows": 200,
             "query_timeout_s": 30, "readonly_default": True}
        )
        glob = {"max_rows": 200, "query_timeout_s": 30, "readonly_default": True}
        entry = cfg["connections"][0]
    else:
        cfg = load_config(agent, project_name, agent_profile)
        glob = cfg
        entry = find_entry(cfg, conn_id)

    if not entry.get("enabled", True):
        raise DbHubError(f"Connection '{entry.get('id')}' is disabled.")

    resolved_dsn = secrets_bridge.resolve_placeholders(
        entry.get("dsn", ""), project_name=scope[0], context=context
    )
    if not resolved_dsn.strip():
        raise DbHubError(f"Connection '{entry.get('id')}' has an empty dsn.")

    driver_name = entry["driver"]
    options = _resolve_options(entry.get("options", {}), scope, context)
    oauth_reference = entry.get('options', {}).get('oauth_credential')
    if oauth_reference:
        from usr.plugins.db_hub.helpers import oauth
        options = await asyncio.to_thread(oauth.resolve_options, driver_name, options, oauth_reference, scope[0])
    sensitive_values = (options.get('oauth_access_token'),)
    async def connect_driver(driver):
        try:
            await driver.connect(options, resolved_dsn)
        except Exception:
            if oauth_reference:
                raise DbHubError('OAuth database connection failed. Check its resource URL, permissions, driver availability, and reconnect if needed.') from None
            raise
    if not pool:
        driver = registry.create_driver(driver_name)
        await connect_driver(driver)
        return BoundConnection(driver, entry, glob, scope, context, sensitive_values)

    # asyncpg pools belong to their creating loop. Background memorization and
    # foreground recall can use different framework threads/loops.
    key = (scope[0], scope[1], entry["id"], asyncio.get_running_loop())
    fingerprint = _fingerprint(entry)
    if options.get('oauth_access_token'):
        import hashlib
        fingerprint += hashlib.sha256(options['oauth_access_token'].encode()).hexdigest()
    with _POOL_GUARD:
        pooled = _POOLS.get(key)
        if pooled is None:
            pooled = _Pooled()
            _POOLS[key] = pooled
    async with pooled.lock:
        if pooled.driver is not None and pooled.fingerprint != fingerprint:
            try:
                await pooled.driver.close()
            except Exception:
                pass
            pooled.driver = None
        if pooled.driver is None:
            driver = registry.create_driver(driver_name)
            await connect_driver(driver)
            pooled.driver = driver
            pooled.fingerprint = fingerprint
        pooled.last_used = time.monotonic()
        return BoundConnection(pooled.driver, entry, glob, scope, context, sensitive_values)


async def evict_idle(max_idle_s: float = IDLE_EVICT_S) -> int:
    now = time.monotonic()
    evicted = 0
    with _POOL_GUARD:
        keys = list(_POOLS.keys())
    for key in keys:
        pooled = _POOLS.get(key)
        if pooled is None:
            continue
        if pooled.loop is not asyncio.get_running_loop():
            if pooled.loop.is_closed():
                _POOLS.pop(key, None)
            elif max_idle_s < 0 and pooled.loop.is_running():
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                    _close_pooled(pooled), pooled.loop))
            continue
        async with pooled.lock:
            if pooled.driver is not None and now - pooled.last_used > max_idle_s:
                try:
                    await pooled.driver.close()
                except Exception:
                    pass
                pooled.driver = None
                evicted += 1
    return evicted


async def _close_pooled(pooled):
    async with pooled.lock:
        if pooled.driver is not None:
            await pooled.driver.close()
            pooled.driver = None


async def invalidate_all() -> None:
    await close_detached_pools(detach_all_pools())


def detach_all_pools():
    """Take the old generation before scheduling asynchronous cleanup.

    A synchronous config hook must call this immediately; a coroutine scheduled
    for later could otherwise close connections created after the config save.
    """
    with _POOL_GUARD:
        old = list(_POOLS.values())
        _POOLS.clear()
    return old


async def close_detached_pools(pools):
    for pooled in pools:
        if pooled.loop.is_closed():
            continue
        if pooled.loop is asyncio.get_running_loop():
            await _close_pooled(pooled)
        elif pooled.loop.is_running():
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_close_pooled(pooled), pooled.loop))


async def close_all() -> None:
    await invalidate_all()

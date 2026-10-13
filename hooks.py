"""Framework lifecycle + config hooks for the db_hub plugin.

Runs in the Agent Zero framework runtime. Delegates to helpers.runtime so the
logic stays testable; this module only exposes the hook entry points.
"""

from usr.plugins.db_hub.helpers import runtime


def install():
    runtime.install_dependencies()


def pre_update():
    runtime.close_all_pools_sync()


def uninstall():
    runtime.remove_owned_state()


def get_plugin_config(default=None, hook_context=None, **kwargs):
    return runtime.normalize_for_read(default or {}, hook_context=hook_context)


def save_plugin_config(settings=None, default=None, hook_context=None, project_name='', agent_profile='', **kwargs):
    normalized = runtime.normalize_for_save(settings if settings is not None else default or {})
    from usr.plugins.db_hub.helpers.memory_setup import prepare_sync
    prepare_sync(normalized, project_name, agent_profile)
    runtime.invalidate_all_pools_sync()
    return normalized

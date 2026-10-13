"""Setup banner for db_hub: shown only while no connections are configured."""

from helpers import plugins
from helpers.extension import Extension


class DbHubSetupBanner(Extension):
    async def execute(self, banners: list = [], frontend_context: dict = {},
                      **kwargs):
        try:
            config = plugins.get_plugin_config("db_hub") or {}
        except Exception:
            return
        if (config.get("connections") or []):
            return
        banners.append({
            "id": "db_hub-setup",
            "type": "feature",
            "title": "Database Hub",
            "description": "Connect agents to Postgres, Neo4j, HydraDB, Qdrant, "
                           "Snowflake, LanceDB, and Convex with secret-safe "
                           "named connections.",
            "icon": "database",
            "cta_text": "Connect a database",
            "cta_action": "open-plugin-config:db_hub",
            "dismissible": True,
            "priority": 50,
        })

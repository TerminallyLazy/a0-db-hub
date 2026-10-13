import { createStore } from "/js/AlpineStore.js";
import { fetchApi } from "/js/api.js";
import { toastFrontendError, toastFrontendSuccess } from "/components/notifications/notification-store.js";

const PROVIDERS = {
  neon: {
    name: "Neon", console: "https://console.neon.tech/", tokenUrl: "https://neon.com/docs/manage/api-keys",
    tokenLabel: "Neon management API key",
    guidance: "In Neon, choose a project and branch, then click Connect. Choose the database and role and copy the Postgres connection string.",
  },
  supabase: {
    name: "Supabase", console: "https://supabase.com/dashboard", tokenUrl: "https://supabase.com/dashboard/account/tokens",
    tokenLabel: "Supabase personal access token",
    guidance: "In Supabase, open your project and click Connect. Choose Session pooler, copy the connection string, and enter your existing database password. A project API key is not a database password.",
  },
  qdrant: {
    name: "Qdrant", console: "https://cloud.qdrant.io/",
    guidance: "In Qdrant Cloud, choose your cluster. Copy its HTTPS cluster URL and a Database API key. Add the URL below and put the key in the connection's API key field. A Cloud Management Key is a different credential.",
  },
};

function initial(provider = "neon") {
  return { provider, handle: "", token: "", organization: "", projects: [], project: "", branches: [], branch: "", databases: [], database: "", roles: [], role: "", endpoints: [], endpoint: "", resourceNotice: "", databasePassword: "", connectionId: provider, busy: false };
}

export const store = createStore("dbHubProvider", {
  ...initial(),
  generation: 0,
  providers: PROVIDERS,
  get details() { return PROVIDERS[this.provider]; },
  get canImport() {
    return this.handle && this.project && (this.provider === "neon"
      ? this.branch && this.database && this.role
      : this.endpoint && this.databasePassword);
  },
  async api(payload) {
    const scope = globalThis.Alpine?.store("pluginSettingsPrototype") || {};
    const response = await fetchApi("/plugins/db_hub/provider_setup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_name: scope.projectName || "", agent_profile: scope.agentProfileKey || "", handle: this.handle, ...payload }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "Provider setup failed.");
    return result;
  },
  async reset(provider = "neon") {
    const handle = this.handle;
    this.generation++;
    Object.assign(this, initial(provider));
    if (handle) await this.api({ action: "disconnect", handle }).catch(() => {});
  },
  async connect() {
    const generation = this.generation;
    this.busy = true;
    try {
      const result = await this.api({ action: "connect", provider: this.provider, token: this.token, organization: this.organization });
      if (generation !== this.generation) {
        await this.api({ action: "disconnect", handle: result.handle }).catch(() => {});
        return;
      }
      Object.assign(this, { handle: result.handle, projects: result.projects, project: "", branches: [], branch: "", databases: [], roles: [], endpoints: [] });
      if (!result.projects.length) toastFrontendError("No projects were returned. Check the token's organization and project access, or use the provider console.", "Database Hub");
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, "Database Hub"); }
    finally { if (generation === this.generation) { this.token = ""; this.busy = false; } }
  },
  async selectProject() {
    const generation = this.generation;
    Object.assign(this, { branch: "", branches: [], database: "", databases: [], role: "", roles: [], endpoint: "", endpoints: [], resourceNotice: "", databasePassword: "" });
    if (!this.project) return;
    this.busy = true;
    try {
      const result = await this.api({ action: "resources", project: this.project });
      if (generation !== this.generation) return;
      this.branches = result.branches || [];
      this.endpoints = result.endpoints || [];
      if (this.endpoints.length === 1) this.endpoint = this.endpoints[0].id;
    } catch (error) { if (generation === this.generation) { this.resourceNotice = error.message; toastFrontendError(error.message, "Database Hub"); } }
    finally { if (generation === this.generation) this.busy = false; }
  },
  async selectBranch() {
    const generation = this.generation;
    Object.assign(this, { database: "", databases: [], role: "", roles: [] });
    if (!this.branch) return;
    this.busy = true;
    try {
      const result = await this.api({ action: "resources", project: this.project, branch: this.branch });
      if (generation !== this.generation) return;
      this.databases = result.databases || [];
      this.roles = result.roles || [];
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, "Database Hub"); }
    finally { if (generation === this.generation) this.busy = false; }
  },
  async importDraft(config, context) {
    const generation = this.generation;
    if ((config.connections || []).some((item) => item.id === this.connectionId)) {
      toastFrontendError("That connection name already exists. Choose a different name.", "Database Hub");
      return;
    }
    this.busy = true;
    try {
      const result = await this.api({ action: "import", project: this.project, branch: this.branch, database: this.database, role: this.role, endpoint: this.endpoint, database_password: this.databasePassword, connection_id: this.connectionId });
      if (generation !== this.generation) return;
      config.connections.push(result.connection);
      context.openIdx = config.connections.length - 1;
      toastFrontendSuccess(result.notice, "Database Hub");
      await this.reset(this.provider);
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, "Database Hub"); }
    finally { if (generation === this.generation) { this.databasePassword = ""; this.busy = false; } }
  },
});

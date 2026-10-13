import { createStore } from "/js/AlpineStore.js";
import { fetchApi } from "/js/api.js";
import { toastFrontendError, toastFrontendSuccess } from "/components/notifications/notification-store.js";

function initial(provider = 'bigquery') {
  return { provider, clientId: '', clientSecret: '', accountUrl: '', authorizationEndpoint: '', tokenEndpoint: '', scopes: 'openid offline_access', audience: '', tokenAuth: 'post', handle: '', authorizationUrl: '', complete: false, busy: false, dsn: '', connectionId: provider };
}
const EXAMPLES = { bigquery: 'bigquery://project-id/dataset', snowflake: 'snowflake://account/warehouse/database/schema', convex: 'https://your-deployment.convex.cloud', neo4j: 'neo4j+s://your-database/neo4j', mongodb: 'mongodb+srv://your-cluster/appdb', weaviate: 'https://your-cluster.example' };
export const store = createStore('dbHubOAuth', {
  ...initial(), providers: [], generation: 0,
  get details() { return this.providers.find(p => p.id === this.provider) || {}; },
  get callback() { return location.origin + '/plugins/db_hub/webui/oauth-callback.html'; },
  get example() { return EXAMPLES[this.provider] || ''; },
  async api(payload) {
    const scope = globalThis.Alpine?.store('pluginSettingsPrototype') || {};
    const response = await fetchApi('/plugins/db_hub/oauth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_name: scope.projectName || '', agent_profile: scope.agentProfileKey || '', handle: this.handle, ...payload }) });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'OAuth setup failed.');
    return result;
  },
  async init() {
    try { this.providers = (await this.api({ action: 'catalog' })).providers; }
    catch (error) { toastFrontendError(error.message, 'Database Hub'); }
  },
  async reset(provider = 'bigquery') {
    const handle = this.handle;
    this.generation++;
    Object.assign(this, initial(provider));
    if (handle) await this.api({ action: 'disconnect', handle }).catch(() => {});
  },
  async start() {
    const generation = this.generation;
    this.busy = true;
    try {
      const result = await this.api({ action: 'start', provider: this.provider, client_id: this.clientId, client_secret: this.clientSecret, account_url: this.accountUrl, authorization_url: this.authorizationEndpoint, token_url: this.tokenEndpoint, scopes: this.scopes, audience: this.audience, token_auth: this.tokenAuth });
      if (generation !== this.generation) { await this.api({ action: 'disconnect', handle: result.handle }).catch(() => {}); return; }
      this.handle = result.handle; this.authorizationUrl = result.authorization_url;
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, 'Database Hub'); }
    finally { if (generation === this.generation) { this.clientSecret = ''; this.busy = false; } }
  },
  async finish() {
    const generation = this.generation;
    this.busy = true;
    try {
      const result = await this.api({ action: 'status' });
      if (generation !== this.generation) return;
      if (result.status !== 'complete') throw new Error('Finish sign-in in the provider tab, then click Finish sign-in here.');
      if (['neon', 'supabase'].includes(this.provider)) {
        const found = await this.api({ action: 'discovery' });
        if (generation !== this.generation) return;
        const providerStore = globalThis.Alpine.store('dbHubProvider');
        // Discovery already replaced the prior server session; clear only its UI.
        providerStore.generation++;
        Object.assign(providerStore, { provider: this.provider, handle: found.handle, projects: found.projects, project: '', branches: [], branch: '', databases: [], database: '', roles: [], role: '', endpoints: [], endpoint: '', token: '', databasePassword: '', resourceNotice: '', connectionId: this.provider, busy: false });
        globalThis.Alpine.store('dbHub').setupMode = 'projects';
        toastFrontendSuccess('Account connected. Choose a project below.', 'Database Hub');
        await this.reset(this.provider);
      } else this.complete = true;
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, 'Database Hub'); }
    finally { if (generation === this.generation) this.busy = false; }
  },
  async importDraft(config, context) {
    if ((config.connections || []).some(c => c.id === this.connectionId)) { toastFrontendError('Choose a connection name that does not already exist.', 'Database Hub'); return; }
    const generation = this.generation;
    this.busy = true;
    try {
      const result = await this.api({ action: 'import', dsn: this.dsn, connection_id: this.connectionId });
      if (generation !== this.generation) return;
      config.connections.push(result.connection); context.openIdx = config.connections.length - 1;
      toastFrontendSuccess(result.notice, 'Database Hub');
      await this.reset(this.provider);
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, 'Database Hub'); }
    finally { if (generation === this.generation) this.busy = false; }
  },
});

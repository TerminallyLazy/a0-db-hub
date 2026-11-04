import { explorerMethods } from '/plugins/db_hub/webui/explorer-store.js';
import { createStore } from "/js/AlpineStore.js";
import { fetchApi } from "/js/api.js";
import { toastFrontendError } from "/components/notifications/notification-store.js";

export const store = createStore('dbHubWorkspace', Object.defineProperties({
  project: '', profile: '', connections: [], connection: '', memory: {}, pluginEnabled: true,
  resourceFilter: '', resources: [], schema: {}, target: '', statement: '', params: '{}', query: '', vector: '', filter: '{}',
  queryRequestId: '', result: null, busy: false, allowWrites: false, review: null, generation: 0,
  get visibleResources() { const term = this.resourceFilter.trim().toLowerCase(); return this.resources.filter(item => item.name.toLowerCase().includes(term)); },
  get selected() { return this.connections.find(c => c.id === this.connection); },
  get hasQuery() { return this.selected?.capabilities.some(c => ['sql', 'cypher', 'api', 'kv'].includes(c)); },
  get hasVector() { return this.selected?.capabilities.includes('vector'); },
  get columns() { return [...new Set((this.result?.rows || []).flatMap(row => row && typeof row === 'object' && !Array.isArray(row) ? Object.keys(row) : ['value']))]; },
  get memoryLabel() { return this.memory.blocked_reason || (this.memory.external ? 'Memory uses ' + this.memory.connections.join(' + ') + '. Built-in storage is bypassed.' : this.memory.builtin_enabled ? 'Memory uses the built-in store.' : 'Automatic memory is off in this scope.'); },
  get queryHint() { return { spacetimedb: 'Read: SELECT * FROM table_name LIMIT 50 (no bind parameters, ORDER BY or OFFSET). Write: CALL reducer_name with Parameters {"args": [positional arguments]}; read back the table afterward.', mongodb: 'Statement: collection name. Parameters: {"op":"find","filter":{},"limit":50}', convex: 'Statement: function path, such as messages:list. Parameters: {"kind":"query","args":{}}', redis: 'Enter a Redis command, such as DBSIZE.', fluree: 'Enter the ledger name and provide the query in Parameters.' }[this.selected?.driver] || (this.selected?.capabilities.includes('cypher') ? 'Enter one Cypher statement. Use LIMIT to bound results.' : 'Enter one SQL statement. Use LIMIT to bound results.'); },
  text(value) { return value == null ? 'null' : typeof value === 'object' ? JSON.stringify(value) : String(value); },
  json(value) { return JSON.stringify(value, null, 2); },
  rowValue(row, column) { return row && typeof row === 'object' && !Array.isArray(row) ? row[column] : row; },
  object(text) { const result = JSON.parse(text || '{}'); if (!result || Array.isArray(result) || typeof result !== 'object') throw new Error('Parameters and filters must be JSON objects.'); return result; },
  async api(payload) {
    const response = await fetchApi('/plugins/db_hub/workspace', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_name: this.project, agent_profile: this.profile, connection: this.connection, limit: 50, ...payload }) });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'Database request failed.');
    return result;
  },
  async open(scope) {
    this.project = scope?.projectName || ''; this.profile = scope?.agentProfileKey || '';
    await window.openModal('/plugins/db_hub/webui/main.html');
  },
  async init() {
    this.expanded = false;
    const generation = ++this.generation;
    this.resourceFilter = ''; this.connections = []; this.connection = ''; this.resources = []; this.schema = {}; this.result = null; this.review = null; this.resetExplorer(); this.busy = true;
    try {
      const result = await this.api({ action: 'list' });
      if (generation !== this.generation) return;
      this.connections = result.connections; this.memory = result.memory; this.pluginEnabled = result.plugin_enabled;
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, 'Database workspace'); }
    finally { if (generation === this.generation) { this.busy = false; this.queryRequestId = ''; } }
  },
  destroy() { this.expanded = false; this.generation++; this.resetExplorer(); this.project = ''; this.profile = ''; this.result = null; this.statement = ''; this.params = '{}'; this.query = ''; this.vector = ''; this.filter = '{}'; this.review = null; this.busy = false; },
  async select() {
    this.generation++; this.resetExplorer(); this.resourceFilter = ''; this.resources = []; this.schema = {}; this.target = ''; this.result = null; this.review = null; this.allowWrites = false; this.statement = ''; this.params = '{}'; this.query = ''; this.vector = ''; this.filter = '{}';
    if (this.connection) await this.loadSchema();
  },
  async run(payload, schema = false) {
    const generation = this.generation;
    this.busy = true;
    if (payload.action === 'query' && this.selected?.driver === 'postgres' && !payload.allow_writes) {
      this.queryRequestId = globalThis.crypto?.randomUUID?.() || String(Date.now()) + '-' + Math.random();
      payload = { ...payload, request_id: this.queryRequestId };
    }
    if (!schema) this.result = null;
    try {
      const result = await this.api(payload);
      if (generation !== this.generation) return;
      if (schema) { this.schema = result.schema; this.resources = result.resources; }
      else this.result = result;
    } catch (error) { if (generation === this.generation) toastFrontendError(error.message, 'Database workspace'); }
    finally { if (generation === this.generation) { this.busy = false; this.queryRequestId = ''; } }
  },
  async cancelQuery() {
    if (!this.queryRequestId) return;
    try { await this.api({ action: 'cancel', request_id: this.queryRequestId }); }
    catch (error) { toastFrontendError(error.message, 'Database workspace'); }
  },
  async loadSchema() { await this.loadCatalog(); },
  async preview(next = false) { await this.loadPage(next ? 'next' : 'first'); },
  async runQuery() {
    try {
      const payload = { action: 'query', connection: this.connection, statement: this.statement, params: this.object(this.params), allow_writes: this.allowWrites };
      if (this.allowWrites) { this.review = JSON.parse(JSON.stringify(payload)); return; }
      this.tab = 'query'; await this.run(payload);
    } catch (error) { toastFrontendError(error.message, 'Database workspace'); }
  },
  async confirmQuery() {
    if (!this.review || this.review.connection !== this.connection) return;
    const payload = { ...this.review, confirmed: true }; this.review = null;
    this.tab = 'query'; await this.run(payload);
  },
  async search() {
    try { this.tab = 'query'; await this.run({ action: 'search', target: this.target, query: this.query, vector: this.vector.trim() ? JSON.parse(this.vector) : null, filter: this.object(this.filter) }); }
    catch (error) { toastFrontendError(error.message, 'Database workspace'); }
  },
}, Object.getOwnPropertyDescriptors(explorerMethods)));

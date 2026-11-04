import { createStore } from "/js/AlpineStore.js";
import { fetchApi } from "/js/api.js";
import {
  toastFrontendError,
  toastFrontendSuccess,
} from "/components/notifications/notification-store.js";

const API = "/plugins/db_hub/connections";
const LOGO_BASE = "/plugins/db_hub/webui/logos";

// Static union of every known driver (mirrors backend driver_names()).
// Rendered before the async drivers reply arrives so the <select> never
// re-renders from a short fallback list (which reset the view to BigQuery
// while the model still held the right value).
const ALL_DRIVERS = ["bigquery", "chromadb", "convex", "duckdb", "fluree",
  "hydradb", "lancedb", "milvus", "mongodb", "neo4j", "pinecone", "postgres",
  "qdrant", "redis", "snowflake", "spacetimedb", "sqlalchemy", "weaviate"];

// Drivers whose packages install lazily on first use (mirrors OPTIONAL_DEPS).
const LAZY_DRIVERS = new Set(["snowflake", "lancedb", "sqlalchemy", "redis",
  "mongodb", "pinecone", "chromadb", "milvus", "duckdb", "bigquery", "weaviate"]);

const CATALOG = [
  {
    key: "spacetimedb", name: "SpacetimeDB", driver: "spacetimedb",
    logo: `${LOGO_BASE}/spacetimedb.svg`, icon: "public", dark: true,
    blurb: "Published module tables, SQL queries, and transactional reducer calls.",
    dsn: "https://maincloud.spacetimedb.com",
    options: { database: "" },
  },
  {
    key: "postgresql", name: "PostgreSQL", driver: "postgres",
    logo: `${LOGO_BASE}/postgres.svg`, icon: "database",
    blurb: "Self-hosted Postgres or any Postgres-wire database.",
    dsn: "postgresql://app:§§secret(PG_PASS)@localhost:5432/app",
  },
  {
    key: "neon", name: "Neon", driver: "postgres",
    logo: `${LOGO_BASE}/neon.svg`, icon: "database",
    blurb: "Serverless Postgres. Requires SSL in the connection string.",
    dsn: "postgresql://app:§§secret(NEON_PASS)@ep-xxxx.neon.tech/app?sslmode=require",
  },
  {
    key: "supabase", name: "Supabase", driver: "postgres",
    logo: `${LOGO_BASE}/supabase.svg`, icon: "database",
    blurb: "Supabase Postgres, direct or via the Supavisor pooler.",
    dsn: "postgresql://postgres:§§secret(SUPABASE_PASS)@db.xxxx.supabase.co:5432/postgres",
  },
  {
    key: "neo4j", name: "Neo4j", driver: "neo4j",
    logo: `${LOGO_BASE}/neo4j.svg`, icon: "share",
    blurb: "Graph database queried with OpenCypher over Bolt.",
    dsn: "bolt://neo4j:§§secret(NEO4J_PASS)@localhost:7687/neo4j",
  },
  {
    key: "hydradb", name: "HydraDB", driver: "hydradb",
    logo: `${LOGO_BASE}/hydradb.svg`, icon: "share", bleed: true,
    blurb: "Distributed graph store; Bolt-compatible, HTTPS optional.",
    dsn: "bolt://root:§§secret(HYDRA_PASS)@localhost:7687/graph",
  },
  {
    key: "qdrant", name: "Qdrant", driver: "qdrant",
    logo: `${LOGO_BASE}/qdrant.svg`, icon: "filter_center_focus",
    blurb: "Managed or self-hosted vector search collections.",
    dsn: "http://localhost:6333",
  },
  {
    key: "lancedb", name: "LanceDB", driver: "lancedb",
    logo: `${LOGO_BASE}/lancedb.svg`, icon: "layers",
    blurb: "Embedded or cloud vector tables. No server needed locally.",
    dsn: "~/.lancedb",
  },
  {
    key: "snowflake", name: "Snowflake", driver: "snowflake",
    logo: `${LOGO_BASE}/snowflake.svg`, icon: "cloud",
    blurb: "Snowflake SQL warehouses; connector installs on first use.",
    dsn: "snowflake://user:§§secret(SNOWFLAKE_PASS)@account/warehouse/db/schema",
  },
  {
    key: "convex", name: "Convex", driver: "convex",
    logo: `${LOGO_BASE}/convex.svg`, icon: "bolt",
    blurb: "Convex query, mutation, and action functions over HTTPS.",
    dsn: "https://polished-panda-123.convex.cloud",
  },
  {
    key: "generic", name: "Generic SQL", driver: "sqlalchemy",
    logo: null, icon: "database",
    blurb: "Any database with a SQLAlchemy URL.",
    dsn: "sqlite:////data/app.db",
  },
  {
    key: "redis", name: "Redis", driver: "redis",
    logo: `${LOGO_BASE}/redis.svg`, icon: "database", bleed: true,
    blurb: "Key/value store, plus vector search with RediSearch.",
    dsn: "redis://localhost:6379/0",
  },
  {
    key: "pinecone", name: "Pinecone", driver: "pinecone",
    logo: `${LOGO_BASE}/pinecone.svg`, icon: "database", dark: true,
    blurb: "Serverless vector indexes. Needs an API key.",
    dsn: "pinecone://my-index",
  },
  {
    key: "chromadb", name: "ChromaDB", driver: "chromadb",
    logo: `${LOGO_BASE}/chromadb.svg`, icon: "hub",
    blurb: "Embedded or server vector collections.",
    dsn: "~/.chromadb",
  },
  {
    key: "mongodb", name: "MongoDB", driver: "mongodb",
    logo: `${LOGO_BASE}/mongodb.svg`, icon: "database",
    blurb: "Documents plus Atlas Vector Search where configured.",
    dsn: "mongodb://app:§§secret(MONGO_PASS)@localhost:27017/appdb",
  },
  {
    key: "milvus", name: "Milvus", driver: "milvus",
    logo: `${LOGO_BASE}/milvus.svg`, icon: "database",
    blurb: "Scalable vector collections. Needs an existing collection.",
    dsn: "http://localhost:19530",
  },
  {
    key: "duckdb", name: "DuckDB", driver: "duckdb",
    logo: `${LOGO_BASE}/duckdb.svg`, icon: "database",
    blurb: "Embedded analytics over files or in-memory.",
    dsn: "/data/analytics.duckdb",
  },
  {
    key: "bigquery", name: "BigQuery", driver: "bigquery",
    logo: `${LOGO_BASE}/bigquery.svg`, icon: "cloud",
    blurb: "Google Cloud warehouses. Credentials go in options.",
    dsn: "bigquery://my-project/my_dataset",
  },
  {
    key: "weaviate", name: "Weaviate", driver: "weaviate",
    logo: `${LOGO_BASE}/weaviate.svg`, icon: "database",
    blurb: "Vector collections with metadata filters.",
    dsn: "http://localhost:8080",
  },
  {
    key: "fluree", name: "Fluree", driver: "fluree",
    logo: `${LOGO_BASE}/fluree.svg`, icon: "share",
    blurb: "Ledger graph queries over HTTPS. No extra install.",
    dsn: "http://localhost:8090",
  },
  {
    key: "pgvector", name: "pgvector", driver: "postgres",
    logo: `${LOGO_BASE}/postgres.svg`, icon: "database",
    blurb: "Postgres plus the pgvector extension (CREATE EXTENSION vector).",
    dsn: "postgresql://app:§§secret(PG_PASS)@localhost:5432/app",
  },
];

async function callApi(payload) {
  const proto = globalThis.Alpine?.store("pluginSettingsPrototype");
  if (proto?.pluginName === "db_hub") {
    payload = {
      project_name: proto.projectName || "",
      agent_profile: proto.agentProfileKey || "",
      ...payload,
    };
  }
  const response = await fetchApi(API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || "Database Hub request failed.");
  }
  return result;
}

export const store = createStore("dbHub", {
  drivers: {},
  driversLoaded: false,
  memoryCompatibility: { compatible: false, message: "Checking Agent Zero memory compatibility…" },
  testing: null,
  verifyingMemory: false,
  installing: false,
  catalog: CATALOG,

  catalogSearch: '', catalogCategory: 'All', showAllCatalog: false, setupKey: '', setupMode: 'details',
  get setupProduct() { return this.catalog.find(p => p.key === this.setupKey); },
  catalogType(product) {
    return ['postgres','sqlalchemy','duckdb','snowflake','bigquery','spacetimedb'].includes(product.driver) ? 'SQL' : ['qdrant','lancedb','chromadb','pinecone','milvus','weaviate'].includes(product.driver) ? 'Vector' : ['neo4j','hydradb','fluree'].includes(product.driver) ? 'Graph' : 'Document & key-value';
  },
  get visibleCatalog() {
    const popular = ['neon','supabase','postgresql','qdrant','mongodb','redis','neo4j','lancedb'];
    let items = this.catalog.filter(p => (this.catalogCategory === 'All' || this.catalogType(p) === this.catalogCategory) && (p.name + ' ' + p.driver).toLowerCase().includes(this.catalogSearch.trim().toLowerCase()));
    if (!this.catalogSearch && this.catalogCategory === 'All' && !this.showAllCatalog) items = popular.map(key => items.find(p => p.key === key)).filter(Boolean);
    return items;
  },
  async chooseSetup(key) {
    this.setupKey = key; this.setupMode = 'details';
    const provider = globalThis.Alpine.store('dbHubProvider'), oauth = globalThis.Alpine.store('dbHubOAuth');
    await Promise.all([provider.reset(['neon','supabase','qdrant'].includes(key) ? key : 'neon'), oauth.reset(key)]);
  },
  async closeSetup() { this.setupKey = ''; await Promise.all([globalThis.Alpine.store('dbHubProvider').reset(), globalThis.Alpine.store('dbHubOAuth').reset()]); },

  init(config) {
    if (!config) return;
    this._config = config;
    this.memoryCompatibility = { compatible: false, message: "Checking Agent Zero memory compatibility…" };
    this.setupKey = ''; this.catalogSearch = ''; this.catalogCategory = 'All'; this.showAllCatalog = false;
    if (!Array.isArray(config.connections)) config.connections = [];
    if (typeof config.max_rows !== "number") config.max_rows = 200;
    if (typeof config.query_timeout_s !== "number") config.query_timeout_s = 30;
    if (typeof config.readonly_default !== "boolean") config.readonly_default = true;
    if (typeof config.memory_backend !== "object" || config.memory_backend === null) {
      config.memory_backend = { enabled: false, connection: "", prefix: "mem" };
    }
    const mb = config.memory_backend;
    if (typeof mb.enabled !== "boolean") mb.enabled = false;
    if (typeof mb.connection !== "string") mb.connection = "";
    if (typeof mb.prefix !== "string" || !mb.prefix) mb.prefix = "mem";
    for (const conn of config.connections) {
      if (typeof conn.enabled !== "boolean") conn.enabled = true;
      if (typeof conn.options !== "object" || conn.options === null) conn.options = {};
    }
    this.sanitizeDrivers(config);
    this.refreshDrivers();
  },

  cleanup() {
    this.testing = null;
    this._config = null;
  },

  async refreshDrivers() {
    try {
      const result = await callApi({ action: "drivers" });
      this.drivers = result.drivers || {};
      this.memoryCompatibility = result.memory_compatibility || { compatible: false, message: "Memory compatibility could not be verified. Reopen settings and try again." };
      this.driversLoaded = true;
    } catch {
      this.drivers = {};
      this.driversLoaded = false;
      this.memoryCompatibility = { compatible: false, message: "Memory compatibility could not be verified. Reopen settings and try again." };
    }
    if (this._config) this.sanitizeDrivers(this._config);
  },

  sanitizeDrivers(config) {
    for (const conn of config.connections || []) {
      if (!conn || typeof conn !== "object") continue;
      const cleaned = String(conn.driver || "").trim().toLowerCase();
      if (cleaned && cleaned !== conn.driver) conn.driver = cleaned;
    }
  },

  driverKnown(conn) {
    const names = new Set(this.driverNames().map((n) => String(n).toLowerCase()));
    return !!conn && !!conn.driver && names.has(String(conn.driver).toLowerCase());
  },

  driverNames() {
    const live = Object.keys(this.drivers || {});
    const merged = new Set([...ALL_DRIVERS, ...live]);
    return [...merged].sort();
  },

  driverLabel(name) {
    const info = (this.drivers || {})[name] || {};
    const missing = info.available === false ? " (not installed)" : "";
    return (info.title || name) + missing;
  },

  driverHint(driver) {
    const info = (this.drivers || {})[driver] || {};
    return info.description || "Pick the database engine for this connection.";
  },

  dsnExample(driver) {
    const info = (this.drivers || {})[driver] || {};
    return info.dsn_example || "";
  },

  logoFor(conn) {
    const id = String((conn && conn.id) || "").toLowerCase();
    const byId = CATALOG.find((p) => p.logo && id.includes(p.key));
    if (byId) return byId.logo;
    const driver = conn && conn.driver;
    const byDriver = CATALOG.find((p) => p.driver === driver && p.logo);
    return byDriver ? byDriver.logo : null;
  },

  statusFor(product, config) {
    const info = (this.drivers || {})[product.driver];
    if (!this.driversLoaded || !info) {
      return { tone: "unknown", label: "Checking…" };
    }
    if (this.productLive(product, config)) {
      return { tone: "live", label: "Connected" };
    }
    if (info.available === false && LAZY_DRIVERS.has(product.driver)) {
      return { tone: "lazy", label: "Installs on first use" };
    }
    if (info.available === false) {
      return { tone: "missing", label: "Needs install" };
    }
    return { tone: "ready", label: "Ready" };
  },

  productLive(product, config) {
    const conns = (config && config.connections) || [];
    return conns.some((c) => {
      if (!c || c.enabled === false || !c.last_test || c.last_test.ok !== true) return false;
      const id = String(c.id || "").toLowerCase();
      if (id.includes(product.key)) return true;
      if (c.driver !== product.driver) return false;
      if (product.driver === "postgres") {
        return product.key === "postgresql" && !id.includes("neon")
          && !id.includes("supabase") && !id.includes("pgvector");
      }
      return true;
    });
  },

  shortDriver(driver) {
    const names = {
      postgres: "Postgres", neo4j: "Neo4j", hydradb: "HydraDB", qdrant: "Qdrant",
      lancedb: "LanceDB", snowflake: "Snowflake", convex: "Convex", sqlalchemy: "SQL",
      redis: "Redis", mongodb: "MongoDB", pinecone: "Pinecone", chromadb: "ChromaDB",
      milvus: "Milvus", duckdb: "DuckDB", bigquery: "BigQuery", weaviate: "Weaviate",
      fluree: "Fluree", spacetimedb: "SpacetimeDB",
    };
    return names[driver] || String(driver || "?");
  },

  connPill(conn) {
    if (!conn || conn.enabled === false) return { label: "Disabled", off: true };
    const t = conn.last_test;
    if (t && t.ok === true) {
      return {
        label: typeof t.latency_ms === "number" ? `Tested · ${t.latency_ms} ms` : "Tested",
        off: false,
      };
    }
    if (t && t.ok === false) return { label: "Test failed", off: true };
    return { label: this.shortDriver(conn.driver), off: false };
  },

  connEdited(conn) {
    if (conn) conn.last_test = null;
  },

  needsInstall() {
    if (!this.driversLoaded) return false;
    return CATALOG.some((p) => {
      const info = (this.drivers || {})[p.driver] || {};
      return info.available === false && !LAZY_DRIVERS.has(p.driver);
    });
  },

  async installDrivers() {
    if (this.installing) return;
    this.installing = true;
    try {
      const result = await callApi({ action: "install" });
      this.drivers = result.drivers || {};
      this.driversLoaded = true;
      toastFrontendSuccess("Database drivers repaired. Restart Agent Zero before using the rebuilt packages.", "Database Hub");
    } catch (error) {
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.installing = false;
    }
  },

  scopeLabel(proto) {
    if (!proto || !proto.pluginName) return "";
    const parts = [];
    const proj = proto.loadedProjectName || proto.projectName;
    const prof = proto.loadedAgentProfile || proto.agentProfileKey;
    if (proj) parts.push(`project '${proj}'`);
    if (prof) parts.push(`profile '${prof}'`);
    const where = parts.length ? parts.join(" · ") : "global";
    const isDefault = !proto.loadedPath || /default_config/.test(proto.loadedPath);
    return `Editing ${where} settings${isDefault ? " (defaults — nothing saved here yet)" : ""}. `;
  },

  connectionsDesc(config) {
    const count = (config && config.connections ? config.connections.length : 0);
    if (!count) return "Entries agents can query. Test one before saving.";
    return `${count} configured — expand one to edit, test, then Save.`;
  },

  memoryTargets(config) {
    if (!config.memory_backend || typeof config.memory_backend !== "object") {
      config.memory_backend = { enabled: false, connections: [], prefix: "mem" };
    }
    const mb = config.memory_backend;
    if (!Array.isArray(mb.connections)) mb.connections = [];
    return mb.connections;
  },

  memoryHas(config, id) {
    return this.memoryTargets(config).some((t) => t && t.connection === id);
  },

  memoryConnections(config) {
    return (config.connections || []).filter((conn) =>
      conn.enabled !== false && this.drivers[conn.driver]?.memory_supported);
  },

  async verifyMemory() {
    this.verifyingMemory = true;
    try {
      const result = await callApi({ action: "verify_memory" });
      toastFrontendSuccess(`Saved memory settings verified: ${result.results.map((item) => item.connection).join(", ")} — write, recall and deletion passed.`, "Database Hub");
    } catch (error) {
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.verifyingMemory = false;
    }
  },

  toggleMemoryConn(config, id) {
    const targets = this.memoryTargets(config);
    const at = targets.findIndex((t) => t && t.connection === id);
    if (at >= 0) targets.splice(at, 1);
    else targets.push({ connection: id });
  },

  memoryState(config) {
    const mb = (config && config.memory_backend) || {};
    if (!mb.enabled) return "Using built-in memory settings.";
    if (!this.memoryCompatibility.compatible) return this.memoryCompatibility.message;
    const targets = this.memoryTargets(config).map((t) => t.connection).filter(Boolean);
    if (!targets.length) return "Select at least one connection to activate database memory.";
    const conns = (config && config.connections) || [];
    const missing = targets.filter((id) => !conns.some((c) => c.id === id));
    if (missing.length) return `Memory connection '${missing[0]}' is not configured.`;
    const names = targets.map((id) => `'${id}'`).join(", ");
    return `Memory destination: ${names}. Changes take effect after Save.`;
  },

  addFromCatalog(config, key, context) {
    const product = CATALOG.find((p) => p.key === key);
    if (!product) return;
    const taken = new Set((config.connections || []).map((c) => c.id));
    let id = product.key;
    for (let n = 2; taken.has(id); n += 1) id = `${product.key}_${n}`;
    config.connections.push({
      id, driver: product.driver, dsn: product.dsn,
      enabled: true, readonly: null, options: { ...(product.options || {}) },
    });
    const added = config.connections[config.connections.length - 1];
    added.driver = product.driver;
    this.sanitizeDrivers(config);
    const idx = config.connections.length - 1;
    if (context) context.openIdx = idx;
    requestAnimationFrame(() => {
      const card = document.getElementById(`dbhub-conn-${id}`);
      card?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      card?.querySelector('input[spellcheck="false"]')?.focus({ preventScroll: true });
    });
  },

  addConnection(config, context) {
    const taken = new Set((config.connections || []).map((c) => c.id));
    let id = "connection_1";
    for (let n = 2; taken.has(id); n += 1) id = `connection_${n}`;
    config.connections.push({
      id, driver: "postgres", dsn: "", enabled: true, readonly: null, options: {},
    });
    if (context) context.openIdx = config.connections.length - 1;
  },

  removeConnection(config, idx, context) {
    config.connections.splice(idx, 1);
    if (context) context.openIdx = null;
  },

  rawSecretIn(dsn) {
    if (!dsn || dsn.includes("§§secret(")) return false;
    return /:\/\/[^/\s:]+:[^@/\s]+@/.test(dsn);
  },

  convertibleHint(dsn) {
    if (!dsn) return "";
    const text = dsn.trim();
    if (/^(PGPASSWORD=\S+\s+)?psql(\s|$)/.test(text)) {
      return "This looks like a psql command, not a DSN.";
    }
    if (/^mongosh(\s|$)/.test(text)) {
      return "This looks like a mongosh command, not a DSN.";
    }
    if (/^(SNOWSQL_\S+\s+)?snowsql(\s|$)/.test(text)) {
      return "This looks like a snowsql command, not a DSN.";
    }
    if (text.includes("\n") && text.includes("=")) {
      return "This looks like a .env snippet.";
    }
    const scheme = text.split("://")[0].toLowerCase();
    if (text.includes("://") && scheme.includes("+")) {
      return "This looks like a SQLAlchemy URL with a driver suffix.";
    }
    return "";
  },

  suggestSecretKey(conn) {
    const base = String((conn && conn.id) || "db").toUpperCase().replace(/[^A-Z0-9_]/g, "_").replace(/^_+|_+$/g, "") || "DB";
    return `DB_HUB_${base}_PASS`.slice(0, 64);
  },

  seedSecretKey(idx, conn) {
    this._secretKeys = this._secretKeys || {};
    if (this._secretKeys[idx] === undefined) {
      this._secretKeys[idx] = this.suggestSecretKey(conn);
    }
  },

  secretKeyValue(idx, conn) {
    this.seedSecretKey(idx, conn);
    return this._secretKeys[idx];
  },

  pwFor(idx, conn) {
    this._pw = this._pw || {};
    if (!this._pw[idx]) {
      this._pw[idx] = { password: "", key: this.suggestSecretKey(conn) };
    }
    return this._pw[idx];
  },

  async savePassword(config, idx) {
    const conn = config.connections[idx];
    const form = this.pwFor(idx, conn);
    if (!conn || !form.password) {
      toastFrontendError("Type the new password first.", "Database Hub");
      return;
    }
    this.savingPw = idx;
    try {
      const result = await callApi({
        action: "store_secret",
        dsn: conn.dsn || "",
        key: String(form.key || "").trim(),
        password: form.password,
        conn_id: conn.id || "",
      });
      conn.dsn = result.dsn;
      form.password = "";
      form.key = result.key;
      this.connEdited(conn);
      toastFrontendSuccess(
        `Password saved as ${result.key}. Test, then press Save below to keep it.`,
        "Database Hub",
      );
    } catch (error) {
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.savingPw = null;
    }
  },

  suggestOptionKey(conn) {
    const base = String((conn && conn.id) || "db").toUpperCase().replace(/[^A-Z0-9_]/g, "_").replace(/^_+|_+$/g, "") || "DB";
    return `DB_HUB_${base}_API_KEY`.slice(0, 64);
  },

  optKeyValue(idx, conn) {
    this._optKeys = this._optKeys || {};
    if (this._optKeys[idx] === undefined) {
      this._optKeys[idx] = this.suggestOptionKey(conn);
    }
    return this._optKeys[idx];
  },

  canStorePassword(conn) {
    return !conn?.options?.oauth_credential && /:\/\/[^/\s:@]+(?::[^@]*)?@/.test(conn?.dsn || "");
  },

  showApiKey(conn) {
    if (!conn || conn.options?.oauth_credential || !this.needsApiKey(conn.driver)) return false;
    if (conn.driver === "lancedb") return String(conn.dsn || "").startsWith("db://") || !!conn.options?.api_key;
    if (conn.driver === "chromadb") return /^https?:/.test(conn.dsn || "") || !!conn.options?.api_key;
    return true;
  },

  needsApiKey(driver) {
    return ["chromadb", "lancedb", "pinecone", "qdrant", "weaviate", "spacetimedb"]
      .includes(String(driver || ""));
  },

  credentialSlot(conn) { return conn?.driver === "spacetimedb" ? "auth_token" : "api_key"; },

  rawOptionIn(conn) {
    const value = conn && conn.options ? conn.options[this.credentialSlot(conn)] : "";
    return !!value && !String(value).includes("§§secret(");
  },

  async moveOptionSecret(config, idx) {
    const conn = config.connections[idx];
    const raw = conn && conn.options ? String(conn.options[this.credentialSlot(conn)] || "") : "";
    if (!raw || raw.includes("§§secret(")) {
      toastFrontendError("Nothing to move — the key is already a placeholder.", "Database Hub");
      return;
    }
    const keys = (this._optKeys = this._optKeys || {});
    const key = String(keys[idx] || this.suggestOptionKey(conn)).trim();
    this.movingOpt = idx;
    try {
      const result = await callApi({
        action: "store_secret", dsn: "", key, password: raw, conn_id: conn.id || "",
      });
      conn.options[this.credentialSlot(conn)] = `§§secret(${result.key})`;
      delete keys[idx];
      this.connEdited(conn);
      toastFrontendSuccess(
        `API key stored as ${result.key}. Press Save below to keep it.`,
        "Database Hub",
      );
    } catch (error) {
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.movingOpt = null;
    }
  },

  async normalizeDsn(config, idx) {
    const conn = config.connections[idx];
    if (!conn || !conn.dsn) return;
    this.normalizing = idx;
    try {
      const result = await callApi({
        action: "normalize", text: conn.dsn, conn_id: conn.id || "",
        driver: conn.driver || "",
      });
      conn.dsn = result.dsn;
      for (const [key, value] of Object.entries(result.options || {})) {
        conn.options = conn.options || {};
        if (conn.options[key] === undefined || conn.options[key] === "") {
          conn.options[key] = value;
        }
      }
      this.connEdited(conn);
      toastFrontendSuccess(
        ["Converted to a DSN.", ...(result.notes || [])].join(" "),
        "Database Hub",
      );
    } catch (error) {
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.normalizing = null;
    }
  },

  async extractSecret(config, idx) {
    const conn = config.connections[idx];
    if (!conn || !conn.dsn) return;
    const keys = (this._secretKeys = this._secretKeys || {});
    const key = String(keys[idx] || this.suggestSecretKey(conn)).trim();
    if (!key) {
      toastFrontendError("Enter a key name for the secret first.", "Database Hub");
      return;
    }
    this.extracting = idx;
    try {
      const result = await callApi({
        action: "extract_secret", dsn: conn.dsn, key, conn_id: conn.id || "",
      });
      conn.dsn = result.dsn;
      delete keys[idx];
      this.connEdited(conn);
      toastFrontendSuccess(
        `Password stored as ${result.key}. Test, then press Save below to keep it.`,
        "Database Hub",
      );
    } catch (error) {
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.extracting = null;
    }
  },

  readonlyValue(conn) {
    if (conn.readonly === true) return "true";
    if (conn.readonly === false) return "false";
    return "default";
  },

  setReadonly(conn, value) {
    conn.readonly = value === "true" ? true : value === "false" ? false : null;
  },

  optionsText(conn) {
    const opts = conn.options || {};
    return Object.keys(opts).length ? JSON.stringify(opts) : "";
  },

  setOptions(conn, text) {
    const trimmed = (text || "").trim();
    if (!trimmed) {
      conn.options = {};
      return;
    }
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        conn.options = parsed;
      } else {
        throw new Error("must be a JSON object");
      }
    } catch (error) {
      toastFrontendError(`Advanced options ignored: ${error.message}`, "Database Hub");
    }
  },

  async testConnection(config, idx) {
    const conn = config.connections[idx];
    if (!conn) return;
    this.testing = idx;
    try {
      const result = await callApi({ action: "test", connection: conn });
      const detail = result.result || {};
      conn.last_test = {
        ok: !!detail.ok,
        latency_ms: typeof detail.latency_ms === "number" ? detail.latency_ms : null,
        at: new Date().toISOString(),
        error: detail.ok ? "" : String(detail.error || "Connection test failed.").slice(0, 500),
      };
      await this.persistStamp(conn);
      if (detail.ok) {
        toastFrontendSuccess(
          `Connection '${conn.id || idx}' works (${detail.latency_ms ?? "?"} ms). Press Save below to keep any edits.`,
          "Database Hub",
        );
      } else {
        throw new Error(detail.error || "Connection test failed.");
      }
    } catch (error) {
      if (conn && !conn.last_test) {
        conn.last_test = { ok: false, latency_ms: null, at: new Date().toISOString(), error: String(error.message || error).slice(0, 500) };
        await this.persistStamp(conn);
      }
      toastFrontendError(String(error.message || error), "Database Hub");
    } finally {
      this.testing = null;
    }
  },

  async persistStamp(conn) {
    if (!conn || !conn.id || !conn.last_test) return;
    try {
      await callApi({ action: "stamp", id: conn.id, last_test: conn.last_test });
    } catch {
      // Glow stays for this session; the modal Save still persists everything.
    }
  },
});

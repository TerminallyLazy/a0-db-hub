import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

function harness(respond) {
  const calls = [], notices = [];
  const context = vm.createContext({
    createStore: (_name, value) => value,
    Alpine: { store: () => ({ projectName: 'project-scope', agentProfileKey: 'profile-scope' }) },
    fetchApi: async (url, options) => {
      const input = JSON.parse(options.body); calls.push({ url, input });
      const result = await respond(input);
      return { ok: true, json: async () => result };
    },
    toastFrontendError: (message) => notices.push(message),
    toastFrontendSuccess: (message) => notices.push(message),
  });
  const source = readFileSync(new URL('../webui/provider-setup-store.js', import.meta.url), 'utf8');
  vm.runInContext(source.replace(/^import .*;\n/gm, '').replace('export const store =', 'this.store ='), context);
  return { store: context.store, calls, notices };
}

test('Neon discovery cascades to a secret-only draft in the settings scope', async () => {
  const { store, calls } = harness(async (input) => {
    if (input.action === 'connect') return { ok: true, handle: 'temporary', projects: [{ id: 'p', name: 'Project' }] };
    if (input.action === 'resources' && !input.branch) return { ok: true, branches: [{ id: 'b', name: 'Branch' }] };
    if (input.action === 'resources') return { ok: true, databases: [{ id: 'db' }], roles: [{ id: 'role' }] };
    if (input.action === 'import') return { ok: true, connection: { id: 'new_neon', dsn: 'postgresql://user:§§secret(SAVED)@db.example/db' }, notice: 'Saved credential' };
    return { ok: true };
  });
  store.token = 'synthetic-token'; await store.connect();
  assert.equal(store.token, ''); assert.equal(store.projects[0].id, 'p');
  store.project = 'p'; await store.selectProject();
  store.branch = 'b'; await store.selectBranch();
  store.database = 'db'; store.role = 'role'; store.connectionId = 'new_neon';
  assert.ok(store.canImport);
  const config = { connections: [] }, context = {};
  await store.importDraft(config, context);
  assert.equal(context.openIdx, 0); assert.match(config.connections[0].dsn, /§§secret/);
  assert.equal(store.handle, ''); assert.equal(calls.at(-1).input.action, 'disconnect');
  for (const { input } of calls) { assert.equal(input.project_name, 'project-scope'); assert.equal(input.agent_profile, 'profile-scope'); }
});

test('closing setup during account discovery discards late results and disconnects', async () => {
  let complete;
  const pending = new Promise(resolve => { complete = resolve; });
  const { store, calls } = harness(input => input.action === 'connect' ? pending : { ok: true });
  store.token = 'synthetic-token'; const request = store.connect();
  await store.reset('supabase');
  complete({ ok: true, handle: 'late', projects: [{ id: 'p' }] }); await request;
  assert.equal(store.provider, 'supabase'); assert.equal(store.handle, ''); assert.equal(store.token, '');
  assert.equal(calls.at(-1).input.action, 'disconnect'); assert.equal(calls.at(-1).input.handle, 'late');
});

test('provider errors clear secrets and duplicate names do not import', async () => {
  const { store, calls, notices } = harness(async () => ({ ok: false, error: 'No access' }));
  store.token = 'synthetic-token'; await store.connect();
  assert.equal(store.token, ''); assert.equal(store.busy, false); assert.equal(notices[0], 'No access');
  await store.importDraft({ connections: [{ id: 'neon' }] }, {});
  assert.equal(calls.length, 1); assert.match(notices[1], /already exists/);
});

test('a single returned Supabase transaction pooler is selected automatically', async () => {
  const { store } = harness(async () => ({ ok: true, endpoints: [{ id: 'transaction_0', name: 'Transaction pooler' }] }));
  await store.reset('supabase'); store.handle = 'account'; store.project = 'ref';
  await store.selectProject();
  assert.equal(store.endpoint, 'transaction_0');
  store.databasePassword = 'synthetic'; assert.ok(store.canImport);
});

test('missing Supabase endpoints clear stale selection and show console guidance', async () => {
  const { store, notices } = harness(async () => ({ ok: false, error: 'Open the provider console and click Connect.' }));
  await store.reset('supabase'); store.handle = 'account'; store.project = 'ref'; store.endpoint = 'old';
  await store.selectProject();
  assert.equal(store.endpoint, ''); assert.equal(store.endpoints.length, 0); assert.ok(!store.canImport);
  assert.match(store.resourceNotice, /console/); assert.match(notices[0], /console/);
});

import { schema, table, t, SenderError, type InferSchema, type TransactionCtx } from 'spacetimedb/server';

// Memory data stays private. Procedures authorize every operation with ctx.sender.
const memoryCollections = table({ name: 'dbhub_memory_collection' }, {
  key: t.string().primaryKey(), owner: t.identity().index('btree'),
  name: t.string(), dimension: t.u32(),
});
const memoryEntries = table({ name: 'dbhub_memory_entry' }, {
  key: t.string().primaryKey(), collectionKey: t.string().index('btree'),
  id: t.string(), vector: t.array(t.f64()), payload: t.string(),
});
const db = schema({ memoryCollections, memoryEntries });
export default db;
type Tx = TransactionCtx<InferSchema<typeof db>>;

function reply(fn: () => Record<string, unknown>): string {
  try { return JSON.stringify({ ok: true, ...fn() }); }
  catch (error) {
    if (error instanceof SenderError) return JSON.stringify({ ok: false, error: error.message });
    throw error;
  }
}
function collectionKey(ctx: Tx, name: string): string {
  if (!/^[a-zA-Z0-9_]{1,256}$/.test(name)) throw new SenderError('invalid_collection');
  return JSON.stringify([ctx.sender.toHexString(), name]);
}
function collection(ctx: Tx, name: string) {
  const row = ctx.db.memoryCollections.key.find(collectionKey(ctx, name));
  if (!row) throw new SenderError('collection_missing');
  return row;
}
function checkedVector(vector: number[], dimension: number): number {
  if (vector.length !== dimension) throw new SenderError('dimension_mismatch');
  if (!vector.every(Number.isFinite)) throw new SenderError('invalid_vector');
  const norm = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0));
  if (!Number.isFinite(norm)) throw new SenderError('invalid_vector');
  return norm;
}
function object(text: string): Record<string, unknown> {
  if (text.length > 262144) throw new SenderError('payload_too_large');
  let value: unknown;
  try { value = JSON.parse(text); } catch (_) { throw new SenderError('invalid_payload'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new SenderError('invalid_payload');
  return value as Record<string, unknown>;
}
function checkedFilter(text: string) {
  const value = object(text);
  if (Object.keys(value).length > 32 || Object.entries(value).some(([key, item]) =>
    key.startsWith('$') || (item !== null && !['string', 'number', 'boolean'].includes(typeof item)))) {
    throw new SenderError('unsupported_filter');
  }
  return value;
}
function matches(id: string, payload: Record<string, unknown>, filter: Record<string, unknown>) {
  return Object.entries(filter).every(([key, value]) => (key === 'id' ? id : payload[key]) === value);
}

export const dbhub_memory_info = db.procedure(t.string(), ctx => reply(() => ctx.withTx(tx => ({
  protocol: 'a0_db_hub_memory', version: 1, sender: ctx.sender.toHexString(),
  collections: [...tx.db.memoryCollections.owner.filter(ctx.sender)].map(row => ({ name: row.name, dimension: row.dimension })),
}))));

export const dbhub_memory_ensure = db.procedure({ name: t.string(), dimension: t.u32() }, t.string(),
  (ctx, { name, dimension }) => reply(() => ctx.withTx(tx => {
    if (dimension < 1 || dimension > 65536) throw new SenderError('invalid_dimension');
    const key = collectionKey(tx, name);
    const existing = tx.db.memoryCollections.key.find(key);
    if (existing && existing.dimension !== dimension) throw new SenderError('dimension_mismatch');
    if (!existing) tx.db.memoryCollections.insert({ key, owner: ctx.sender, name, dimension });
    return { ready: true, dimension };
  })));

const Item = t.object('MemoryItem', { id: t.string(), vector: t.array(t.f64()), payload: t.string() });
export const dbhub_memory_upsert = db.procedure({ name: t.string(), items: t.array(Item) }, t.string(),
  (ctx, { name, items }) => reply(() => ctx.withTx(tx => {
    const layout = collection(tx, name);
    if (!items.length || items.length > 100) throw new SenderError('invalid_batch');
    for (const item of items) {
      if (!item.id || item.id.length > 256) throw new SenderError('invalid_id');
      checkedVector(item.vector, layout.dimension); object(item.payload);
      const key = JSON.stringify([layout.key, item.id]);
      const row = { key, collectionKey: layout.key, id: item.id, vector: [...item.vector], payload: item.payload };
      if (tx.db.memoryEntries.key.find(key)) tx.db.memoryEntries.key.update(row);
      else tx.db.memoryEntries.insert(row);
    }
    return { upserted: items.length };
  })));

export const dbhub_memory_search = db.procedure(
  { name: t.string(), vector: t.array(t.f64()), topK: t.u32(), filter: t.string(), maxScan: t.u32() }, t.string(),
  (ctx, { name, vector, topK, filter, maxScan }) => reply(() => ctx.withTx(tx => {
    const layout = collection(tx, name), queryNorm = checkedVector(vector, layout.dimension), where = checkedFilter(filter);
    if (topK < 1 || topK > 100 || maxScan < 1 || maxScan > 50000) throw new SenderError('invalid_limit');
    let scanned = 0;
    const found: Array<{ id: string; score: number; payload: Record<string, unknown> }> = [];
    for (const row of tx.db.memoryEntries.collectionKey.filter(layout.key)) {
      if (++scanned > maxScan) throw new SenderError('scan_limit');
      const payload = object(row.payload);
      if (!matches(row.id, payload, where)) continue;
      const norm = checkedVector(row.vector, layout.dimension);
      const dot = row.vector.reduce((sum, value, index) => sum + value * vector[index], 0);
      const score = queryNorm && norm ? Math.max(-1, Math.min(1, dot / norm / queryNorm)) : 0;
      found.push({ id: row.id, score, payload });
      found.sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
      if (found.length > topK) found.pop();
    }
    return { matches: found, scanned };
  })));

export const dbhub_memory_delete = db.procedure(
  { name: t.string(), ids: t.array(t.string()), filter: t.string(), maxScan: t.u32() }, t.string(),
  (ctx, { name, ids, filter, maxScan }) => reply(() => ctx.withTx(tx => {
    const layout = collection(tx, name), where = checkedFilter(filter);
    if (!ids.length && !Object.keys(where).length) throw new SenderError('delete_selector_required');
    if (ids.length > 1000 || maxScan < 1 || maxScan > 50000) throw new SenderError('invalid_limit');
    let deleted = 0;
    if (ids.length) {
      for (const id of new Set(ids)) {
        if (tx.db.memoryEntries.key.delete(JSON.stringify([layout.key, id]))) deleted++;
      }
    } else {
      let scanned = 0;
      for (const row of tx.db.memoryEntries.collectionKey.filter(layout.key)) {
        if (++scanned > maxScan) throw new SenderError('scan_limit');
        if (matches(row.id, object(row.payload), where) && tx.db.memoryEntries.key.delete(row.key)) deleted++;
      }
    }
    return { deleted };
  })));

export const dbhub_memory_list = db.procedure(
  { name: t.string(), limit: t.u32(), offset: t.u32(), maxScan: t.u32() }, t.string(),
  (ctx, { name, limit, offset, maxScan }) => reply(() => ctx.withTx(tx => {
    const layout = collection(tx, name);
    if (limit < 1 || limit > 100 || maxScan < 1 || maxScan > 50000) throw new SenderError('invalid_limit');
    const rows: Array<{ id: string; payload: Record<string, unknown>; vector: number[] }> = [];
    let scanned = 0;
    for (const row of tx.db.memoryEntries.collectionKey.filter(layout.key)) {
      if (++scanned > maxScan) throw new SenderError('scan_limit');
      if (scanned > offset && rows.length <= limit) rows.push({ id: row.id, payload: object(row.payload), vector: row.vector });
    }
    const more = rows.length > limit;
    return { rows: rows.slice(0, limit), next_offset: more ? offset + limit : null };
  })));

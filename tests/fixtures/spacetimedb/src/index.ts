import { schema, table, t, SenderError } from 'spacetimedb/server';
const record = table({name:'record', public:true}, {
  id:t.u64().primaryKey().autoInc(), owner:t.identity(), text:t.string(),
});
const privateRecord = table({name:'private_record'}, {id:t.u32().primaryKey(), text:t.string()});
const db = schema({record, privateRecord});
export default db;
export const init = db.init(ctx => {ctx.db.privateRecord.insert({id:1,text:'synthetic private fixture'});});
export const add_record = db.reducer({text:t.string()}, (ctx,{text}) => {
  if (!text) throw new SenderError('text required');
  ctx.db.record.insert({id:0n,owner:ctx.sender,text});
});
export const add_exact_record = db.reducer({id:t.u64(),text:t.string()}, (ctx,{id,text}) => {
  ctx.db.record.insert({id,owner:ctx.sender,text});
});
export const rename_record = db.reducer({id:t.u64(),text:t.string()}, (ctx,{id,text}) => {
  const row=ctx.db.record.id.find(id);
  if (!row || !row.owner.equals(ctx.sender)) throw new SenderError('unauthorized');
  ctx.db.record.id.update({...row,text});
});
export const delete_record = db.reducer({id:t.u64()}, (ctx,{id}) => {
  const row=ctx.db.record.id.find(id);
  if (!row || !row.owner.equals(ctx.sender)) throw new SenderError('unauthorized');
  ctx.db.record.id.delete(id);
});
export const fail_record = db.reducer({text:t.string()}, (ctx,{text}) => {
  ctx.db.record.insert({id:0n,owner:ctx.sender,text});
  throw new SenderError('intentional rollback');
});

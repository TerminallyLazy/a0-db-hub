import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
function harness(respond = async () => ({ok:true,rows:[]})) {
 const calls=[], notices=[];
 const context=vm.createContext({createStore:(_name,value)=>value,toastFrontendError:message=>notices.push(message),fetchApi:async(url,opts)=>{const input=JSON.parse(opts.body);calls.push(input);return {ok:true,json:async()=>respond(input)};}});
 const explorer=readFileSync(new URL('../webui/explorer-store.js',import.meta.url),'utf8');
 vm.runInContext(explorer.replace(/^import .*;\n/gm,'').replace('export const explorerMethods =','const explorerMethods ='),context);
 const source=readFileSync(new URL('../webui/workspace-store.js',import.meta.url),'utf8');
 vm.runInContext(source.replace(/^import .*;\n/gm,'').replace('export const store =','this.store ='),context);
 return {store:context.store,calls,notices};
}
test('read queries retain saved scope; changes require review of an immutable request',async()=>{
 const {store,calls}=harness(); store.project='proj';store.profile='profile';store.connection='pg';store.statement='SELECT 1';
 await store.runQuery(); assert.equal(calls[0].allow_writes,false); assert.equal(calls[0].project_name,'proj');
 store.allowWrites=true;store.statement='UPDATE t SET n = :n';store.params='{"n":1}';await store.runQuery();assert.equal(calls.length,1);
 store.statement='DELETE FROM t';store.params='{"n":9}';await store.confirmQuery();
 assert.equal(calls[1].statement,'UPDATE t SET n = :n');assert.equal(calls[1].params.n,1);assert.equal(calls[1].confirmed,true);
});
test('a closed or changed workspace ignores stale results and clears query data',async()=>{
 let complete;const response=new Promise(resolve=>{complete=resolve;});const {store}=harness(()=>response);
 store.statement='private query';const pending=store.run({action:'query'});store.destroy();await Promise.resolve();complete({ok:true,rows:[{private:'old result'}]});await pending;
 assert.equal(store.result,null);assert.equal(store.statement,'');assert.equal(store.busy,false);
});
test('invalid JSON sends no query and connection changes discard review',async()=>{
 const {store,calls,notices}=harness();store.params='[]';await store.runQuery();assert.equal(calls.length,0);assert.match(notices[0],/JSON objects/);
 store.review={connection:'old',statement:'DELETE FROM x'};store.connection='new';await store.confirmQuery();assert.equal(calls.length,0);
});
test('resource tree groups schemas and places public first without changing full names',()=>{
 const {store}=harness();store.resources=[{name:'neon_auth.user',parts:['neon_auth','user'],group:'neon_auth',label:'user'},{name:'public.notes',parts:['public','notes'],group:'public',label:'notes'}];
 assert.equal(store.groups[0].name,'public');assert.equal(store.groups[1].items[0].label,'user');store.resourceFilter='notes';assert.equal(store.groups.length,1);
});
test('data paging preserves native cursors and does not replace a changed connection',async()=>{
 let complete;const response=new Promise(resolve=>{complete=resolve;});const {store,calls}=harness(input=>input.action==='page'?response:{ok:true});store.connection='qdrant';store.target='points';store.family='vector';store.dataResult={rows:[],next_cursor:0};
 const pending=store.loadPage('next');store.generation++;store.dataResult=null;complete({ok:true,rows:[{id:1}],next_cursor:5});await pending;assert.equal(calls[0].cursor,0);assert.equal(store.dataResult,null);
});
test('record mutation applies the reviewed token instead of mutable editor contents',async()=>{
 const {store,calls}=harness(input=>input.action==='review'?{ok:true,token:'signed-review',request:input}:input.action==='apply'?{ok:true,affected:1}:{ok:true,rows:[]});store.connection='pg';store.target='public.t';store.family='relational';store.inspector={record:{id:1,name:'before'},identity:{key:{id:1},version:'old'}};store.editOperation='update';store.editText='{"id":1,"name":"after"}';await store.reviewRecord();
 assert.deepEqual(calls[0].values,{name:'after'});store.editText='{"name":"different"}';await store.applyRecord();assert.equal(calls[1].token,'signed-review');assert.equal(calls[1].values,undefined);assert.equal(calls[2].action,'page');
});

test('diagram retains late relationship columns and routes edges inside their cards',()=>{
 const {store}=harness();const columns=Array.from({length:30},(_,i)=>({name:'column_'+i,type:'int'}));
 store.diagram={tables:[{resource:'wide',columns,primary_key:['column_0']},{resource:'other',columns:[{name:'id',type:'int'}],primary_key:['id']}],edges:[{source:'wide',target:'other',columns:['column_29'],target_columns:['id']}]};
 store.diagramPositions={wide:{x:0,y:0},other:{x:300,y:0}};
 assert.equal(store.tableColumns(store.diagram.tables[0]).length,19);assert.equal(store.tableColumns(store.diagram.tables[0])[18].name,'column_29');
 assert.match(store.visibleEdges[0].path,/M248,504/);assert.ok(store.tableHeight(store.diagram.tables[0])>504);
});

test('table creation uses immutable reviewed token and refreshes the new resource',async()=>{
 const {store,calls}=harness(input=>input.action==='review_table'?{ok:true,token:'table-token',connection:'pg',statement:'CREATE TABLE notes (...)'}:input.action==='apply'?{ok:true,created:true,resource:'public.notes'}:input.action==='catalog'?{ok:true,family:'relational',resources:[{name:'public.notes',parts:['public','notes'],group:'public',label:'notes'}]}:{ok:true,columns:[],rows:[]});
 store.connection='pg';store.tableDefinition={schema:'public',name:'notes',columns:[]};await store.reviewTable();store.tableDefinition.name='changed';await store.createTable();
 assert.equal(calls[1].token,'table-token');assert.equal(calls[1].definition,undefined);assert.equal(store.target,'public.notes');assert.equal(store.tab,'structure');
});
test('agent prompts include scope and resource while preserving memory and saved permissions',()=>{
 const {store}=harness();store.connections=[{id:'pg',driver:'postgres',readonly:true,capabilities:['sql']}];store.connection='pg';store.project='project-a';store.profile='researcher';store.target='public.notes';store.agentTask='import';
 assert.match(store.agentPrompt,/project-a/);assert.match(store.agentPrompt,/public.notes/);assert.match(store.agentPrompt,/read-only permissions/);assert.match(store.agentPrompt,/do not change memory settings/);store.resetExplorer();assert.equal(store.tableReview,null);assert.equal(store.agentHelp,false);
});

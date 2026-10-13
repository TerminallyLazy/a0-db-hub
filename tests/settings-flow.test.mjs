import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
function harness() {
 const reset=[];const context=vm.createContext({createStore:(_,s)=>s,requestAnimationFrame:()=>{},toastFrontendSuccess:()=>{},toastFrontendError:()=>{},Alpine:{store:name=>({reset:async provider=>reset.push([name,provider])})}});
 const source=readFileSync(new URL('../webui/db-hub-store.js',import.meta.url),'utf8').replace(/^import [\s\S]*? from [^;]+;\n/gm,'').replace('export const store =','this.store =');vm.runInContext(source,context);return {store:context.store,reset};
}
test('catalog selection only opens relevant setup; adding a draft is deliberate',async()=>{
 const {store,reset}=harness();const config={connections:[{id:'neon',driver:'postgres',dsn:'existing'}]};await store.chooseSetup('supabase');assert.equal(config.connections.length,1);assert.equal(store.setupProduct.name,'Supabase');assert.equal(reset[0][1],'supabase');assert.equal(reset[1][1],'supabase');
 const state={};store.addFromCatalog(config,'supabase',state);assert.equal(config.connections.length,2);assert.equal(config.connections[1].driver,'postgres');assert.equal(state.openIdx,1);assert.equal(config.connections[0].dsn,'existing');
});
test('catalog search covers all engines even when popular providers are shown',()=>{
 const {store}=harness();assert.equal(store.visibleCatalog.length,8);store.catalogSearch='milvus';assert.equal(store.visibleCatalog[0].key,'milvus');store.catalogSearch='';store.catalogCategory='Graph';assert.ok(store.visibleCatalog.every(p=>['neo4j','hydradb','fluree'].includes(p.key)));store.catalogCategory='All';store.showAllCatalog=true;assert.equal(store.visibleCatalog.length,store.catalog.length);
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';
function harness(respond=async()=>({ok:true})) {
 const calls=[],reset=[],lookups=[];const panel={open:false,scrollIntoView:()=>{},querySelector:()=>({focus:()=>{}})};const card={open:false,querySelector:()=>panel};
 const context=vm.createContext({document:{getElementById:id=>{lookups.push(id);return card;}},fetchApi:async(_,options)=>{const data=JSON.parse(options.body);calls.push(data);return {ok:true,json:async()=>await respond(data)};},createStore:(_,s)=>s,requestAnimationFrame:()=>{},toastFrontendSuccess:()=>{},toastFrontendError:()=>{},Alpine:{nextTick:callback=>Promise.resolve().then(callback),store:name=>({reset:async provider=>reset.push([name,provider])})}});
 const source=readFileSync(new URL('../webui/db-hub-store.js',import.meta.url),'utf8').replace(/^import [\s\S]*? from [^;]+;\n/gm,'').replace('export const store =','this.store =');vm.runInContext(source,context);return {store:context.store,reset,calls,card,panel,lookups};
}
test('catalog selection only opens relevant setup; adding a draft is deliberate',async()=>{
 const {store,reset}=harness();const config={connections:[{id:'neon',driver:'postgres',dsn:'existing'}]};await store.chooseSetup('supabase');assert.equal(config.connections.length,1);assert.equal(store.setupProduct.name,'Supabase');assert.equal(reset[0][1],'supabase');assert.equal(reset[1][1],'supabase');
 const state={};store.addFromCatalog(config,'supabase',state);assert.equal(config.connections.length,2);assert.equal(config.connections[1].driver,'postgres');assert.equal(state.openIdx,1);assert.equal(config.connections[0].dsn,'existing');
});
test('catalog search covers all engines even when popular providers are shown',()=>{
 const {store}=harness();assert.equal(store.visibleCatalog.length,8);store.catalogSearch='milvus';assert.equal(store.visibleCatalog[0].key,'milvus');store.catalogSearch='';store.catalogCategory='Graph';assert.ok(store.visibleCatalog.every(p=>['neo4j','hydradb','fluree'].includes(p.key)));store.catalogCategory='All';store.showAllCatalog=true;assert.equal(store.visibleCatalog.length,store.catalog.length);
});

test('SpacetimeDB catalog adds independent database options and uses the auth token secret slot',()=>{
 const {store}=harness();store.catalogSearch='spacetime';store.catalogCategory='SQL';assert.equal(store.visibleCatalog[0].driver,'spacetimedb');
 const config={connections:[]};store.addFromCatalog(config,'spacetimedb',{});store.addFromCatalog(config,'spacetimedb',{});
 assert.equal(config.connections[0].options.database,'');config.connections[0].options.database='published-db';assert.equal(config.connections[1].options.database,'');
 const conn=config.connections[0];assert.ok(store.driverNames().includes('spacetimedb'));assert.ok(store.showApiKey(conn));assert.equal(store.credentialSlot(conn),'auth_token');
 conn.options.auth_token='synthetic-token';assert.ok(store.rawOptionIn(conn));conn.options.auth_token='§§secret(ST_TOKEN)';assert.equal(store.rawOptionIn(conn),false);
});

test('memory toggle permits SpacetimeDB setup while preserving core compatibility gates',()=>{
 const {store}=harness();const config={connections:[{id:'st',driver:'spacetimedb',options:{}}],memory_backend:{enabled:false}};
 assert.equal(store.memoryToggleDisabled(config),true);store.driversLoaded=true;store.memoryCompatibility.compatible=true;store.drivers={spacetimedb:{memory_supported:true}};
 assert.equal(store.memoryToggleDisabled(config),false);assert.match(store.memoryAvailability(config),/Set up memory/);assert.equal(store.memoryConnections(config).length,0);
 config.connections[0].options.memory_database='private';assert.equal(store.memoryConnections(config).length,1);assert.equal(store.memoryToggleDisabled(config),false);
 store.memoryCompatibility.compatible=false;assert.equal(store.memoryToggleDisabled(config),true);config.memory_backend.enabled=true;assert.equal(store.memoryToggleDisabled(config),false);
});

test('turning on memory moves the draft switch and opens the sole SpacetimeDB setup without writes',async()=>{
 const {store,calls,card,panel,lookups}=harness();const state={openIdx:null};
 const config={connections:[{id:'st',driver:'spacetimedb',options:{database:'app'}}],memory_backend:{enabled:false,connections:[]}};
 store._config=config;store.driversLoaded=true;store.memoryCompatibility.compatible=true;store.drivers={spacetimedb:{memory_supported:true}};
 await store.memoryToggleChanged(config,state,true);
 assert.equal(config.memory_backend.enabled,true);assert.equal(config.memory_backend.connections[0].connection,'st');
 assert.equal(state.openIdx,0);assert.equal(card.open,true);assert.equal(panel.open,true);assert.deepEqual(lookups,['dbhub-conn-st']);
 assert.equal(config.connections[0].options.memory_database,undefined);assert.equal(calls.length,0);
 await store.memoryToggleChanged(config,state,false);assert.equal(config.memory_backend.enabled,false);assert.equal(calls.length,0);
});

test('multiple memory destinations require selection and the setup shortcut opens the chosen connection',async()=>{
 const {store,calls,lookups}=harness();const state={};const config={connections:[{id:'one',driver:'spacetimedb',options:{}},{id:'two',driver:'spacetimedb',options:{}}],memory_backend:{enabled:false,connections:[]}};
 store._config=config;store.driversLoaded=true;store.memoryCompatibility.compatible=true;store.drivers={spacetimedb:{memory_supported:true}};
 await store.memoryToggleChanged(config,state,true);assert.equal(config.memory_backend.enabled,true);assert.equal(config.memory_backend.connections.length,0);assert.equal(lookups.length,0);
 await store.openSpacetimeMemorySetup(config,config.connections[1],state);assert.equal(state.openIdx,1);assert.deepEqual(lookups,['dbhub-conn-two']);assert.equal(calls.length,0);
});

test('a delayed setup reveal cannot reopen a closed or changed settings page',async()=>{
 const {store,lookups}=harness();const config={connections:[{id:'st',driver:'spacetimedb',options:{}}]};store._config=config;
 const opening=store.openSpacetimeMemorySetup(config,config.connections[0],{});store.cleanup();await opening;assert.equal(lookups.length,0);
});

test('existing prepared memory toggles normally without opening setup or changing selected stores',async()=>{
 const {store,calls,lookups}=harness();const config={connections:[{id:'pg',driver:'postgres',options:{}}],memory_backend:{enabled:false,connections:[{connection:'pg'}]}};
 store._config=config;store.driversLoaded=true;store.memoryCompatibility.compatible=true;store.drivers={postgres:{memory_supported:true}};
 await store.memoryToggleChanged(config,{},true);assert.equal(config.memory_backend.enabled,true);assert.equal(config.memory_backend.connections.length,1);
 assert.equal(lookups.length,0);assert.equal(calls.length,0);
 store.memoryCompatibility.compatible=false;await store.memoryToggleChanged(config,{},false);assert.equal(config.memory_backend.enabled,false);
 await store.memoryToggleChanged(config,{},true);assert.equal(config.memory_backend.enabled,false);
});

test('existing supported memory drivers remain eligible and disabled connections are excluded',()=>{
 const {store}=harness();store.drivers=Object.fromEntries(['postgres','qdrant','lancedb','chromadb'].map(driver=>[driver,{memory_supported:true}]));
 const config={connections:Object.keys(store.drivers).map(driver=>({id:driver,driver}))};assert.equal(store.memoryConnections(config).length,4);
 config.connections[0].enabled=false;assert.equal(store.memoryConnections(config).length,3);
});

test('reviewed creation only attaches storage to the draft and never saves settings',async()=>{
 const {store,calls}=harness(async data=>data.action==='review_spacetime_memory'?{ok:true,token:'once'}:{ok:true,ready:true,memory_database:'private'});
 const config={connections:[{id:'st',driver:'spacetimedb',options:{database:'app'}}]};store._config=config;
 await store.reviewSpacetimeMemory(config,0);assert.equal(store.memorySetupFor(config.connections[0]).review.token,'once');
 await store.createSpacetimeMemory(config,0);assert.equal(config.connections[0].options.memory_database,'private');
 assert.deepEqual(calls.map(x=>x.action),['review_spacetime_memory','create_spacetime_memory']);assert.equal(calls[0].connection.options.memory_database,undefined);
 await store.createSpacetimeMemory(config,0);assert.equal(calls.length,2);
});

test('stale review results cannot authorize a changed connection or a new settings page',async()=>{
 let finish;const pending=new Promise(resolve=>finish=resolve);const {store,calls}=harness(async()=>pending);
 const config={connections:[{id:'st',driver:'spacetimedb',options:{database:'app'}}]};store._config=config;
 const reviewing=store.reviewSpacetimeMemory(config,0);config.connections[0].options.database='changed';finish({ok:true,token:'stale'});await reviewing;
 assert.equal(store.memorySetupFor(config.connections[0]).review,null);await store.createSpacetimeMemory(config,0);assert.equal(calls.length,1);
});

test('recovery attaches the previously created database without another create call',async()=>{
 const {store,calls}=harness(async()=>({ok:true,recovery:'existing'}));const config={connections:[{id:'st',driver:'spacetimedb',options:{}}]};store._config=config;
 await store.reviewSpacetimeMemory(config,0);store.attachSpacetimeMemory(config,0);assert.equal(config.connections[0].options.memory_database,'existing');assert.equal(calls.length,1);
});

test('creation finishing after a draft or page change cannot attach storage to the new draft',async()=>{
 for(const change of ['connection','page']){
  let finish;const pending=new Promise(resolve=>finish=resolve);const {store}=harness(async data=>data.action==='review_spacetime_memory'?{ok:true,token:'once'}:pending);
  const config={connections:[{id:'st',driver:'spacetimedb',options:{database:'app'}}]};store._config=config;
  await store.reviewSpacetimeMemory(config,0);const creating=store.createSpacetimeMemory(config,0);
  if(change==='connection')config.connections[0].dsn='https://another-server';else store._config={connections:[]};
  finish({ok:true,ready:true,memory_database:'previous-server-memory'});await creating;
  assert.equal(config.connections[0].options.memory_database,undefined);
 }
});

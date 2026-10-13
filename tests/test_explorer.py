import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,AsyncMock,patch
from usr.plugins.db_hub.helpers.explorer import service
from usr.plugins.db_hub.helpers.explorer.common import DbHubError,plain,digest
from usr.plugins.db_hub.helpers.explorer.relational import SQLAlchemy,Warehouse
from usr.plugins.db_hub.helpers.explorer.vectors import Vectors
from usr.plugins.db_hub.helpers.explorer.native import Mongo,Redis,Graph,Functions
from usr.plugins.db_hub.api.explorer import Explorer

def setUpModule():
 from usr.plugins.db_hub.helpers.bootstrap import ensure_deps_path
 ensure_deps_path()

class ExplorerContract(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  service._REVIEWS.clear()
  self.bound=NS(conn_cfg={'id':'test','driver':'postgres','readonly':False},glob={},_timeout=lambda:30,mask=lambda x:x)
  self.adapter=NS(catalog=AsyncMock(return_value={'resources':[{'name':'t','parts':['t']}]}),metadata=AsyncMock(return_value={'capabilities':{'insert':True,'update':True,'delete':True}}),mutate=AsyncMock(return_value={'affected':1}))
 def context(self):return patch.multiple(service,adapter=Mock(return_value=self.adapter))
 async def execute(self,data,owner='one'):
  with self.context(),patch.object(service.workspace,'enabled_plugins',return_value=['db_hub']),patch.object(service.registry,'get_connection',AsyncMock(return_value=self.bound)):
   return await service.execute({'connection':'test','target':'t',**data},'project','profile',owner)
 async def test_review_scope_exact_snapshot_single_use_and_readonly_recheck(self):
  source={'n':1};r=await self.execute({'action':'review','operation':'update','values':source,'identity':{'key':1}});source['n']=9
  with self.assertRaises(DbHubError):await self.execute({'action':'apply','token':r['token']},'stranger')
  await self.execute({'action':'apply','token':r['token'],'values':{'n':99}})
  self.assertEqual(self.adapter.mutate.call_args.args[1]['values'],{'n':1})
  with self.assertRaises(DbHubError):await self.execute({'action':'apply','token':r['token']})
  r=await self.execute({'action':'review','operation':'delete','values':{}});self.bound.conn_cfg['readonly']=True
  with self.assertRaises(DbHubError):await self.execute({'action':'apply','token':r['token']})
  self.assertEqual(self.adapter.mutate.await_count,1)
 async def test_identifiers_are_discovered_and_mutations_are_capability_gated(self):
  with self.assertRaises(DbHubError):await self.execute({'action':'page','target':'t; DROP TABLE x'})
  self.adapter.metadata.return_value={'capabilities':{}}
  with self.assertRaises(DbHubError):await self.execute({'action':'review','operation':'delete','values':{}})
 def test_api_protected_and_large_integers_preserved(self):
  self.assertTrue(Explorer.requires_auth());self.assertTrue(Explorer.requires_csrf());self.assertEqual(plain({'id':2**60}),{'id':str(2**60)})

class RelationalTests(unittest.IsolatedAsyncioTestCase):
 async def test_duckdb_readonly_rejects_hidden_writes_and_retains_data(self):
  import duckdb
  from usr.plugins.db_hub.helpers.drivers.duckdb import DuckDriver
  from usr.plugins.db_hub.helpers.base import ReadOnlyError
  driver=DuckDriver();driver._conn=duckdb.connect(':memory:')
  try:
   driver._conn.execute('CREATE TABLE protected(id INTEGER)')
   driver._conn.execute('INSERT INTO protected VALUES (1)')
   for statement in ('SELECT 1; DROP TABLE protected', 'SELECT 1; DELETE FROM protected', 'EXPLAIN ANALYZE DROP TABLE protected'):
    with self.subTest(statement=statement):
     with self.assertRaises(ReadOnlyError):await driver.query(statement,{},True)
     self.assertEqual(driver._conn.execute('SELECT * FROM protected').fetchall(),[(1,)])
   self.assertEqual((await driver.query('SELECT * FROM protected;',{},True))['rows'],[{'id':1}])
  finally:await driver.close()

 async def test_sqlalchemy_filter_sort_composite_identity_cas_and_injection(self):
  import sqlalchemy as sa
  from sqlalchemy.pool import StaticPool
  engine=sa.create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
  with engine.begin() as c:
   c.exec_driver_sql('CREATE TABLE parent (tenant INTEGER, id INTEGER, name TEXT, PRIMARY KEY(tenant,id))')
   c.exec_driver_sql('CREATE TABLE child (id INTEGER PRIMARY KEY, tenant INTEGER, parent INTEGER, FOREIGN KEY(tenant,parent) REFERENCES parent(tenant,id))')
   c.exec_driver_sql("INSERT INTO parent VALUES (1,1,'Alpha'),(1,2,'Beta'),(2,1,'A 100%')")
  a=SQLAlchemy(NS(driver=NS(_engine=engine)));resources=(await a.catalog())['resources'];r=next(x for x in resources if x['label']=='parent');meta=await a.metadata(r)
  self.assertEqual(meta['primary_key'],['tenant','id']);child=await a.metadata(next(x for x in resources if x['label']=='child'));self.assertEqual(child['foreign_keys'][0]['target_columns'],['tenant','id'])
  page=await a.page(r,{'limit':1,'filters':[{'column':'name','op':'contains','value':'A'}],'sort':{'column':'id','direction':'desc'}},meta);self.assertEqual(len(page['rows']),1);self.assertEqual(page['next_offset'],1)
  identity=page['identities'][0];await a.mutate(r,{'operation':'update','values':{'name':'Changed'},'identity':identity},meta)
  with self.assertRaises(DbHubError):await a.mutate(r,{'operation':'delete','identity':identity},meta)
  with self.assertRaises(DbHubError):await a.page(r,{'filters':[{'column':'name;drop table parent','op':'eq','value':'x'}]},meta)
  self.assertEqual(len((await a.page(r,{'filters':[{'column':'name','op':'contains','value':'%'}]},meta))['rows']),1)
  await a.mutate(r,{'operation':'insert','values':{'tenant':3,'id':7,'name':'Added'}},meta)
  latest=await a.page(r,{'filters':[{'column':'tenant','op':'eq','value':3}]},meta);await a.mutate(r,{'operation':'delete','identity':latest['identities'][0]},meta);engine.dispose()
 async def test_duckdb_schema_pages_and_atomic_conflict(self):
  import duckdb
  c=duckdb.connect(':memory:');c.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY,name VARCHAR)');c.execute("INSERT INTO parent VALUES(1,'one'),(2,'two')")
  d=NS(_conn=c);d.list_schema=AsyncMock(return_value={'tables':[{'schema':'main','table':'parent'}]})
  a=Warehouse(NS(driver=d,conn_cfg={'driver':'duckdb'}));r=(await a.catalog())['resources'][0];m=await a.metadata(r);self.assertEqual(m['primary_key'],['id'])
  p=await a.page(r,{'limit':1,'sort':{'column':'id','direction':'desc'}},m);self.assertEqual(p['rows'][0]['id'],2)
  await a.mutate(r,{'operation':'update','values':{'name':'new'},'identity':p['identities'][0]},m)
  with self.assertRaises(DbHubError):await a.mutate(r,{'operation':'delete','identity':p['identities'][0]},m)
  c.close()

class VectorTests(unittest.IsolatedAsyncioTestCase):
 async def test_qdrant_named_vectors_page_inspect_conflict_and_delete(self):
  from qdrant_client import QdrantClient,models
  client=QdrantClient(':memory:');client.create_collection('test',vectors_config={'text':models.VectorParams(size=3,distance=models.Distance.COSINE)})
  d=NS(_client=client,_options={});a=Vectors(NS(driver=d,conn_cfg={'driver':'qdrant'}));r={'name':'test'};meta=await a.metadata(r);self.assertEqual(meta['dimension'],{'text':3})
  value={'id':1,'payload':{'tag':'a'},'vector':{'text':[1.,0.,0.]}}
  await a.mutate(r,{'operation':'insert','values':value},meta)
  with self.assertRaises(DbHubError):await a.mutate(r,{'operation':'insert','values':value},meta)
  p=await a.page(r,{'native_filter':{'must':[{'key':'tag','match':{'value':'a'}}]}},meta);self.assertEqual(p['rows'][0]['id'],1)
  rec=await a.record(r,{'id':1});identity={'id':1,'version':digest(rec)}
  await a.mutate(r,{'operation':'update','values':{**value,'payload':{'tag':'b'}},'identity':identity},meta)
  with self.assertRaises(DbHubError):await a.mutate(r,{'operation':'delete','identity':identity},meta)
  rec=await a.record(r,{'id':1});await a.mutate(r,{'operation':'delete','identity':{'id':1,'version':digest(rec)}},meta);self.assertIsNone(await a.record(r,{'id':1}));client.close()
 async def test_lance_upsert_does_not_duplicate_ids(self):
  import lancedb
  with tempfile.TemporaryDirectory() as path:
   db=lancedb.connect(path);db.create_table('records',[{'id':'a','vector':[1.,0.,0.],'text':'one'}]);d=NS(_db=db,_table=db.open_table,_options={});a=Vectors(NS(driver=d,conn_cfg={'driver':'lancedb'}));r={'name':'records'};m=await a.metadata(r);rec=await a.record(r,{'id':'a'})
   await a.mutate(r,{'operation':'update','identity':{'id':'a','version':digest(rec)},'values':{**rec,'payload':{'text':'two'}}},m)
   self.assertEqual(db.open_table('records').count_rows(),1);self.assertEqual((await a.record(r,{'id':'a'}))['payload']['text'],'two')
 async def test_pinecone_cursor_namespace_and_capabilities(self):
  idx=Mock();idx.list_paginated.return_value={'vectors':[{'id':'a'}],'pagination':{'next':'cursor'}};idx.fetch.return_value={'vectors':{'a':{'id':'a','metadata':{'x':1},'values':[0.1,0.2]}}}
  d=NS(_get_index=AsyncMock(return_value=idx),_options={});a=Vectors(NS(driver=d,conn_cfg={'driver':'pinecone'}));result=await a.page({'name':'idx'},{'namespace':'ns','cursor':'old','limit':5},{})
  self.assertEqual(result['next_cursor'],'cursor');self.assertEqual(result['identities'][0]['namespace'],'ns');self.assertEqual(idx.list_paginated.call_args.kwargs['limit'],5)
 def test_dimensions_and_finite_values(self):
  a=Vectors(NS(driver=Mock(),conn_cfg={'driver':'qdrant'}))
  for vector in ([1],[float('nan'),0],[True,0]):
   with self.assertRaises(DbHubError):a.validate({'id':1,'payload':{},'vector':vector},{'dimension':2})

class NativeTests(unittest.IsolatedAsyncioTestCase):
 async def test_graph_uses_parameters_and_exact_original_properties(self):
  d=NS(query=AsyncMock(return_value={'rows':[{'id':'a','properties':{'name':'x'}}]}));a=Graph(NS(driver=d));r={'name':'Person','parts':['Person'], 'kind':'label'}
  await a.page(r,{'native_filter':{'name':'x" MATCH (n) DELETE n'},'limit':2},{'columns':[]})
  statement,params,ro=d.query.call_args.args;self.assertTrue(ro);self.assertNotIn('DELETE n',statement);self.assertEqual(params['v0'],'x" MATCH (n) DELETE n')
  await a.mutate(r,{'operation':'update','values':{'properties':{'name':'y'}},'identity':{'id':'a','original':{'name':'x'}}},{})
  statement,params,ro=d.query.call_args.args;self.assertFalse(ro);self.assertIn('properties(n)=$original',statement)
 async def test_redis_uses_scan_and_retains_zero_or_integer_cursor(self):
  c=Mock();c.scan.return_value=(17,[b'a']);c.pipeline.return_value.execute.return_value=[b'string',-1]
  a=Redis(NS(driver=NS(_client=c,_options={},_decode=lambda x:x.decode() if isinstance(x,bytes) else x)));r=await a.page({}, {'cursor':'0','native_filter':{'pattern':'a*'}},{})
  self.assertEqual(r['rows'][0]['key'],'a');self.assertEqual(r['next_cursor'],'17');c.keys.assert_not_called()
 async def test_fluree_ledgers_and_native_transact(self):
  from usr.plugins.db_hub.helpers.drivers.fluree import FlureeDriver
  d=FlureeDriver();d._options={'ledger':'test'};response=Mock(status_code=200);response.json.return_value={'ok':True};d._client=NS(post=AsyncMock(return_value=response));a=Functions(NS(driver=d,conn_cfg={'driver':'fluree'}));self.assertEqual((await a.catalog())['resources'][0]['name'],'test')
  with self.assertRaises(DbHubError):await d.query('test',{'kind':'transact','query':{'insert':[{'@id':'test'}]}},True)
  await d.query('test',{'kind':'transact','query':{'insert':[{'@id':'test'}]}},False);self.assertEqual(d._client.post.call_args.args[0],'/fluree/transact')

class ProviderFixtures(unittest.IsolatedAsyncioTestCase):
 async def test_chroma_records_metadata_replacement_and_dimension(self):
  import chromadb
  with tempfile.TemporaryDirectory() as path:
   client=chromadb.PersistentClient(path);client.create_collection('fixture');d=NS(_client=client,_options={});a=Vectors(NS(driver=d,conn_cfg={'driver':'chromadb'}));r={'name':'fixture'};m=await a.metadata(r)
   val={'id':'one','vector':[1.,0.,0.],'payload':{'old':'a'},'text':'test'};await a.mutate(r,{'operation':'insert','values':val},m)
   rec=await a.record(r,{'id':'one'});await a.mutate(r,{'operation':'update','values':{**val,'payload':{'new':'b'}},'identity':{'id':'one','version':digest(rec)}},m)
   updated=await a.record(r,{'id':'one'});self.assertEqual(updated['payload'],{'new':'b'});self.assertEqual((await a.metadata(r))['dimension'],3)
 async def test_bigquery_declared_foreign_keys_are_unenforced(self):
  info=NS(schema=[NS(name='id',field_type='INTEGER',mode='REQUIRED',fields=[])],table_type='TABLE',to_api_repr=lambda:{'tableConstraints':{'primaryKey':{'columns':['id']},'foreignKeys':[{'name':'parent','referencedTable':{'datasetId':'dataset','tableId':'parent'},'columnReferences':[{'referencingColumn':'id','referencedColumn':'id'}]}]}})
  d=NS(_client=NS(get_table=Mock(return_value=info)),_project='proj');a=Warehouse(NS(driver=d,conn_cfg={'driver':'bigquery'}));m=await a.metadata({'name':'dataset.t','parts':['dataset','t'],'kind':'table'})
  self.assertFalse(m['foreign_keys'][0]['enforced']);self.assertFalse(m['capabilities']['update']);self.assertTrue(m['capabilities']['insert'])
 async def test_milvus_page_respects_primary_id_and_omits_vector(self):
  client=Mock();client.query.return_value=[{'pk':1,'vector':[1,2],'text':'one'},{'pk':2,'vector':[3,4],'text':'two'}];a=Vectors(NS(driver=NS(_client=client,_options={}),conn_cfg={'driver':'milvus'}));m={'columns':[{'name':'pk','primary':True},{'name':'text'}]}
  page=await a.page({'name':'collection'},{'limit':1,'native_filter':{'text':'one'}},m);self.assertEqual(page['identities'][0]['id'],1);self.assertNotIn('vector',page['rows'][0]['payload']);self.assertEqual(page['next_offset'],1)
 async def test_weaviate_cursor_and_payload_inspection(self):
  col=Mock();col.query.fetch_objects.return_value=NS(objects=[NS(uuid='id1',properties={'name':'one'}),NS(uuid='id2',properties={'name':'two'})]);d=NS(_client=NS(collections=NS(get=Mock(return_value=col))),_options={});a=Vectors(NS(driver=d,conn_cfg={'driver':'weaviate'}));page=await a.page({'name':'Doc'},{'limit':1,'cursor':'previous'},{})
  self.assertEqual(page['next_cursor'],'id1');self.assertEqual(col.query.fetch_objects.call_args.kwargs['after'],'previous')
 async def test_mongo_update_matches_full_original_and_preserves_id(self):
  col=Mock();col.replace_one.return_value=NS(matched_count=1);a=Mongo(NS(driver=Mock()));a.collection=lambda r:col;a.decode=lambda x:x;a.clean=lambda x:x
  identity={'id':'original','original':{'_id':'original','value':1}};await a.mutate({}, {'operation':'update','identity':identity,'values':{'_id':'original','value':2}}, {})
  self.assertEqual(col.replace_one.call_args.args[0]['$expr']['$eq'][1],{'$literal':identity['original']})
  col.replace_one.return_value=NS(matched_count=0)
  with self.assertRaises(DbHubError):await a.mutate({}, {'operation':'update','identity':identity,'values':{'_id':'original','value':3}}, {})
 async def test_graph_topology_uses_provider_edges(self):
  d=NS(query=AsyncMock(side_effect=[{'rows':[{'nodes':[{'_id':'1','name':'Person'},{'_id':'2','name':'Team'}],'relationships':[{'_start':'1','_end':'2','_type':'MEMBER'}]}]},{'rows':[{'nodeLabels':['Person'],'propertyName':'name','propertyTypes':['String']}]}]));a=Graph(NS(driver=d));result=await a.diagram([])
  self.assertEqual(result['edges'][0]['source'],'Person');self.assertEqual(result['edges'][0]['target'],'Team');self.assertEqual(result['tables'][0]['columns'][0]['name'],'name')

"""Native vector resource browsing. Schemaless payloads are explicitly sampled."""
import asyncio
import json
import math
import re
from .common import *

class Vectors:
    def __init__(self,bound): self.driver=bound.driver;self.kind=bound.conn_cfg['driver']
    async def catalog(self):
        from usr.plugins.db_hub.helpers.workspace import resources
        raw=await self.driver.list_schema();items=resources(raw)
        for r in items:r.update(group='Indexes' if self.kind=='pinecone' else 'Collections',label=r['name'],kind='collection')
        return dict(resources=items[:1000],family='vector',truncated=len(items)>1000,visual=False)
    async def metadata(self,r):
        d=self.driver;k=self.kind;t=r['name'];columns=[];config={};dimension=None
        if k=='qdrant':
            info=await asyncio.to_thread(d._client.get_collection,t);config=plain(info);v=info.config.params.vectors
            dimension={name:x.size for name,x in v.items()} if isinstance(v,dict) else v.size
            columns=[dict(name=name,type=str(getattr(info,'data_type','payload index'))) for name,info in (info.payload_schema or {}).items()]
        elif k=='lancedb':
            table=await asyncio.to_thread(d._table,t);columns=[dict(name=f.name,type=str(f.type),nullable=f.nullable) for f in table.schema];config={'schema':str(table.schema),'version':await asyncio.to_thread(lambda:table.version)}
            field=d._options.get('vector_column','vector');typ=table.schema.field(field).type if field in table.schema.names else None;dimension=getattr(typ,'list_size',None)
        elif k=='chromadb':
            col=await asyncio.to_thread(d._client.get_collection,t);sample=await asyncio.to_thread(col.get,limit=1,include=['embeddings']);emb=sample.get('embeddings');dimension=len(emb[0]) if emb is not None and len(emb) else None
            config={'metadata':col.metadata,'count':await asyncio.to_thread(col.count),'configuration':getattr(col,'configuration',None)}
        elif k=='pinecone':
            idx=await d._get_index(t);config=plain(await asyncio.to_thread(idx.describe_index_stats));dimension=config.get('dimension');config['index']=plain(await asyncio.to_thread(d._client.describe_index,t))
        elif k=='milvus':
            config=await asyncio.to_thread(d._client.describe_collection,t);columns=[dict(name=f['name'],type=str(f.get('type')),primary=bool(f.get('is_primary'))) for f in config.get('fields',[])];dimension=next((f.get('params',{}).get('dim') for f in config.get('fields',[]) if f['name']==d._options.get('vector_field','vector')),None)
        elif k=='weaviate':
            col=d._client.collections.get(t);info=await asyncio.to_thread(col.config.get);columns=[dict(name=p.name,type=str(p.data_type)) for p in info.properties];config=info.to_dict() if hasattr(info,'to_dict') else str(info)
        return dict(resource=t,columns=columns,primary_key=['id'],foreign_keys=[],indexes=[],configuration=config,dimension=dimension,capabilities=caps(page='cursor',filter=k!='pinecone',sort=False,insert=k!='lancedb' or any(c['name']=='id' for c in columns),update=k!='lancedb' or any(c['name']=='id' for c in columns),delete=k!='lancedb' or any(c['name']=='id' for c in columns),note='Records are checked again before applying changes. Avoid editing the same record in another app at the same time.'))
    def lance_where(self,flt,table):
        parts=[]
        for key,value in obj(flt).items():
            if key not in table.schema.names:raise DbHubError('Unknown filter field.')
            if isinstance(value,(dict,list)):raise DbHubError('Use scalar equality filters.')
            col=quote(key);val="'"+str(value).replace("'","''")+"'" if isinstance(value,str) else str(value).lower()
            parts.append(col+' IS NULL' if value is None else col+' = '+val)
        return ' AND '.join(parts)
    async def page(self,r,data,meta):
        k=self.kind;d=self.driver;t=r['name'];n=limit(data);cursor=data.get('cursor');flt=obj(data.get('native_filter') or {});off=offset(data)
        if k=='qdrant':
            from qdrant_client import models
            native=models.Filter(**flt) if flt else None
            points,next_cursor=await asyncio.to_thread(d._client.scroll,collection_name=t,limit=n,offset=cursor,scroll_filter=native,with_payload=True,with_vectors=False)
            return dict(rows=[{'id':p.id,'payload':p.payload} for p in points],identities=[{'id':p.id} for p in points],next_cursor=next_cursor)
        if k=='pinecone':
            idx=await d._get_index(t);namespace=str(data.get('namespace') or d._options.get('namespace',''));page=plain(await asyncio.to_thread(idx.list_paginated,limit=n,pagination_token=cursor,namespace=namespace));ids=[x['id'] for x in page.get('vectors',[])]
            fetched=plain(await asyncio.to_thread(idx.fetch,ids=ids,namespace=namespace)) if ids else {};rows=[dict(id=x['id'],payload=x.get('metadata',{})) for x in fetched.get('vectors',{}).values()]
            return dict(rows=rows,identities=[dict(id=x['id'],namespace=namespace) for x in rows],next_cursor=page.get('pagination',{}).get('next'),note='ID listing requires a serverless index. Use vector search or an exact ID on pod indexes.')
        def read():
            if k=='lancedb':
                table=d._table(t);query=table.search();where=self.lance_where(flt,table)
                if where:query=query.where(where)
                raw=query.offset(off).limit(n+1).to_list();field=d._options.get('vector_column','vector');rows=[dict(id=x.get('id'),payload={key:v for key,v in x.items() if key not in ('id',field)}) for x in raw[:n]]
                return dict(rows=rows,identities=[{'id':x['id']} if x['id'] is not None else None for x in rows],next_offset=off+n if len(raw)>n else None)
            if k=='chromadb':
                col=d._client.get_collection(t);raw=col.get(limit=n+1,offset=off,where=flt or None,include=['metadatas','documents']);rows=[dict(id=x,payload=(raw.get('metadatas') or [None]*len(raw['ids']))[i],text=(raw.get('documents') or [None]*len(raw['ids']))[i]) for i,x in enumerate(raw['ids'][:n])]
                return dict(rows=rows,identities=[{'id':x['id']} for x in rows],next_offset=off+n if len(raw['ids'])>n else None)
            if k=='milvus':
                fields={f['name'] for f in meta['columns']};clauses=[]
                for key,value in flt.items():
                    if key not in fields or not re.fullmatch(r'[A-Za-z_][\w]*',key):raise DbHubError('Unknown filter field.')
                    clauses.append(key+' == '+json.dumps(value))
                rows=d._client.query(collection_name=t,filter=' and '.join(clauses),output_fields=['*'],limit=n+1,offset=off);idfield=next((f['name'] for f in meta['columns'] if f.get('primary')),d._options.get('id_field','id'));vf=d._options.get('vector_field','vector')
                result=[dict(id=x[idfield],payload={key:v for key,v in x.items() if key not in (idfield,vf)}) for x in rows[:n]]
                return dict(rows=result,identities=[{'id':x['id']} for x in result],next_offset=off+n if len(rows)>n else None)
            if k=='weaviate':
                col=d._client.collections.get(t);kwargs=dict(limit=n+1,include_vector=False)
                if flt:kwargs.update(filters=d._filter(flt),offset=off)
                elif cursor:kwargs['after']=cursor
                raw=col.query.fetch_objects(**kwargs).objects;rows=[dict(id=str(x.uuid),payload=x.properties) for x in raw[:n]]
                return dict(rows=rows,identities=[{'id':x['id']} for x in rows],next_offset=off+n if flt and len(raw)>n else None,next_cursor=rows[-1]['id'] if not flt and len(raw)>n else None)
        return await asyncio.to_thread(read)
    async def record(self,r,identity):
        d=self.driver;k=self.kind;t=r['name'];item=identity.get('id')
        if item is None:raise DbHubError('An exact record ID is required.')
        if k=='qdrant':
            pts=await asyncio.to_thread(d._client.retrieve,collection_name=t,ids=[item],with_payload=True,with_vectors=True);return dict(id=pts[0].id,payload=pts[0].payload,vector=pts[0].vector) if pts else None
        if k=='pinecone':
            idx=await d._get_index(t);raw=plain(await asyncio.to_thread(idx.fetch,ids=[str(item)],namespace=identity.get('namespace',d._options.get('namespace',''))));p=raw.get('vectors',{}).get(str(item));return dict(id=item,payload=p.get('metadata'),vector=p.get('values')) if p else None
        def read():
            if k=='chromadb':
                raw=d._client.get_collection(t).get(ids=[str(item)],include=['documents','metadatas','embeddings'])
                if not raw['ids']:return None
                return dict(id=item,text=(raw.get('documents') or [None])[0],payload=(raw.get('metadatas') or [None])[0],vector=plain(raw['embeddings'][0]) if raw.get('embeddings') is not None else None)
            if k=='lancedb':
                table=d._table(t);rows=table.search().where(self.lance_where({'id':item},table)).limit(2).to_list()
                if len(rows)>1:raise DbHubError('Duplicate IDs: use Query or repair the collection before editing.')
                if not rows:return None
                row=rows[0];return dict(id=row.pop('id'),vector=row.pop(d._options.get('vector_column','vector'),None),payload=row)
            if k=='milvus':
                rows=d._client.get(collection_name=t,ids=[item]);vf=d._options.get('vector_field','vector');idfield=d._options.get('id_field','id')
                if not rows:return None
                row=rows[0];return dict(id=row.pop(idfield,item),vector=row.pop(vf,None),payload=row)
            if k=='weaviate':
                p=d._client.collections.get(t).query.fetch_object_by_id(item,include_vector=True)
                if not p:return None
                vec=p.vector;vec=vec['default'] if isinstance(vec,dict) and set(vec)=={'default'} else vec
                return dict(id=str(p.uuid),payload=p.properties,vector=vec)
        return await asyncio.to_thread(read)
    def validate(self,values,meta):
        values=obj(values);obj(values.get('payload') or {});vector=values.get('vector');dimensions=meta.get('dimension')
        vectors=vector if isinstance(vector,dict) else {'':vector}
        if isinstance(dimensions,dict) and set(vectors)!=set(dimensions):raise DbHubError('Provide every named vector from Structure.')
        for name,vec in vectors.items():
            if not isinstance(vec,list) or not vec or len(vec)>65536 or not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in vec):raise DbHubError('Vectors must contain finite numbers (maximum 65,536 dimensions).')
            dim=dimensions.get(name) if isinstance(dimensions,dict) else dimensions
            if dim is not None and len(vec)!=int(dim):raise DbHubError(f'Expected {dim} vector dimensions; received {len(vec)}.')
        if values.get('id') is None:raise DbHubError('Choose an explicit record ID.')
        return values
    async def mutate(self,r,data,meta):
        d=self.driver;k=self.kind;t=r['name'];op=data['operation'];identity=data.get('identity') or {};values=obj(data.get('values',{}));item=identity.get('id') if op!='insert' else values.get('id')
        current=await self.record(r,{**identity,'id':item})
        if op=='insert' and current is not None:raise DbHubError('That ID already exists. Inspect it to review an update.')
        if op!='insert' and (current is None or digest(current)!=identity.get('version')):conflict()
        if op!='delete':
            values=self.validate(values,meta)
            if values['id']!=item:raise DbHubError('The record ID cannot be changed.')
        if k=='pinecone':
            idx=await d._get_index(t);namespace=identity.get('namespace',d._options.get('namespace',''))
            if op=='delete':await asyncio.to_thread(idx.delete,ids=[str(item)],namespace=namespace)
            else:await asyncio.to_thread(idx.upsert,vectors=[dict(id=str(item),values=values['vector'],metadata=values.get('payload') or {})],namespace=namespace)
        elif k=='qdrant':
            from qdrant_client import models
            if op=='delete':await asyncio.to_thread(d._client.delete,collection_name=t,points_selector=models.PointIdsList(points=[item]),wait=True)
            else:await asyncio.to_thread(d._client.upsert,collection_name=t,points=[models.PointStruct(id=item,vector=values['vector'],payload=values.get('payload') or {})],wait=True)
        elif k=='lancedb':
            def write():
                table=d._table(t)
                if op=='delete':table.delete(self.lance_where({'id':item},table))
                else:
                    row={**values.get('payload',{}),'id':item,d._options.get('vector_column','vector'):values['vector']}
                    table.merge_insert('id').when_matched_update_all().when_not_matched_insert_all().execute([row])
            await asyncio.to_thread(write)
        elif k=='milvus':
            if op=='delete':await asyncio.to_thread(d._client.delete,collection_name=t,ids=[item])
            else:await asyncio.to_thread(d._client.upsert,collection_name=t,data=[{**values.get('payload',{}),d._options.get('id_field','id'):item,d._options.get('vector_field','vector'):values['vector']}])
        elif k=='weaviate':
            col=d._client.collections.get(t)
            if op=='delete':await asyncio.to_thread(col.data.delete_by_id,item)
            elif op=='insert':await asyncio.to_thread(col.data.insert,uuid=item,properties=values.get('payload') or {},vector=values['vector'])
            else:await asyncio.to_thread(col.data.replace,uuid=item,properties=values.get('payload') or {},vector=values['vector'])
        elif k=='chromadb':
            col=await asyncio.to_thread(d._client.get_collection,t)
            if op=='delete':await asyncio.to_thread(col.delete,ids=[str(item)])
            else:
                kwargs=dict(ids=[str(item)],embeddings=[values['vector']])
                payload=values.get('payload') or {}
                if op=='update':payload={**{key:None for key in (current.get('payload') or {}) if key not in payload},**payload}
                if payload:kwargs['metadatas']=[payload]
                if values.get('text') is not None:kwargs['documents']=[str(values['text'])]
                await asyncio.to_thread(col.update if op=='update' else col.add,**kwargs)
        elif op=='delete':await d.vector_delete(t,[item])
        else:await d.vector_upsert(t,[values])
        return dict(affected=1,rows=[])

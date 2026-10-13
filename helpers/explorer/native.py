"""Document, graph, keyspace and function-native explorer workflows."""
import asyncio
import json
from .common import *

class Mongo:
    def __init__(self,b):self.driver=b.driver
    async def catalog(self):
        d=self.driver
        def read():
            db=d._db if d._db is not None else d._client.get_default_database(default=None)
            databases=[db.name] if db is not None else d._client.list_database_names()[:50];items=[]
            for name in databases:
                if name in ('admin','config','local'):continue
                for col in d._client[name].list_collection_names():items.append(dict(name=name+'.'+col,parts=[name,col],group=name,label=col,kind='collection'))
            return dict(resources=items[:1000],truncated=len(items)>1000,family='document',visual=False)
        return await asyncio.to_thread(read)
    def collection(self,r):return self.driver._client[r['parts'][0]][r['parts'][1]]
    def decode(self,x):
        from bson import json_util
        return json_util.loads(json.dumps(x))
    def clean(self,x):
        from bson import json_util
        return json.loads(json_util.dumps(x,json_options=json_util.CANONICAL_JSON_OPTIONS))
    async def metadata(self,r):
        def read():
            c=self.collection(r);rows=list(c.find().limit(25));types={}
            for row in rows:
                for k,v in row.items():types.setdefault(k,set()).add(type(v).__name__)
            return dict(resource=r['name'],columns=[dict(name=k,type=' / '.join(sorted(v)),sampled=True) for k,v in types.items()],sample_size=len(rows),primary_key=['_id'],foreign_keys=[],indexes=self.clean(list(c.list_indexes())),configuration=self.clean(c.options()),capabilities=caps(insert=True,update=True,delete=True,note='Field types are sampled; indexes and validators come from the server. JSON uses MongoDB Extended JSON to preserve BSON types.'))
        return await asyncio.to_thread(read)
    async def page(self,r,data,meta):
        native=self.decode(obj(data.get('native_filter') or {}));sort=data.get('sort') or {};projection=data.get('projection') or None
        def reject(x):
            if isinstance(x,dict):
                if {'$where','$function','$accumulator'} & set(x):raise DbHubError('Server-side JavaScript filters are not available in the explorer.')
                for v in x.values():reject(v)
            elif isinstance(x,list):
                for v in x:reject(v)
        reject(native)
        def read():
            n=limit(data);off=offset(data);c=self.collection(r);cursor=c.find(native,projection).sort([(sort.get('column') or '_id',-1 if sort.get('direction')=='desc' else 1)]).skip(off).limit(n+1);rows=list(cursor)
            return dict(rows=self.clean(rows[:n]),identities=[dict(id=self.clean(x['_id'])) if '_id' in x else None for x in rows[:n]],next_offset=off+n if len(rows)>n else None)
        return await asyncio.to_thread(read)
    async def record(self,r,identity):
        result=await asyncio.to_thread(self.collection(r).find_one,{'_id':self.decode(identity['id'])});return self.clean(result) if result else None
    async def mutate(self,r,data,meta):
        c=self.collection(r);op=data['operation'];vals=self.decode(obj(data.get('values',{})));identity=data.get('identity') or {}
        def write():
            if op=='insert':return dict(affected=1,id=self.clean(c.insert_one(vals).inserted_id),rows=[])
            original=self.decode(obj(identity.get('original')));key=self.decode(identity.get('id'))
            if original.get('_id')!=key:raise DbHubError('Refresh the document before editing.')
            condition={'_id':key,'$expr':{'$eq':['$$ROOT',{'$literal':original}]}}
            if op=='delete':result=c.delete_one(condition);count=result.deleted_count
            else:
                if vals.get('_id')!=key:raise DbHubError('The _id cannot be changed.')
                result=c.replace_one(condition,vals);count=result.matched_count
            if count!=1:conflict()
            return dict(affected=count,rows=[])
        return await asyncio.to_thread(write)

class Redis:
    def __init__(self,b):self.driver=b.driver
    async def catalog(self):return dict(resources=[dict(name='keys',parts=['keys'],group='Keyspace',label='Keys',kind='keyspace')],family='keyvalue',visual=False)
    async def metadata(self,r):
        config=await self.driver.list_schema()
        return dict(resource='keys',columns=[dict(name=x,type=t) for x,t in [('key','string'),('type','Redis type'),('ttl','seconds')]],primary_key=['key'],foreign_keys=[],configuration=config,indexes=config.get('search_indexes',[]),capabilities=caps(page='cursor',sort=False,insert=True,update=True,delete=True,note='SCAN is bounded and may return an empty page or repeated keys during concurrent writes. Inspect values up to 256 KB / 200 members; larger values are browse-only.'))
    async def page(self,r,data,meta):
        d=self.driver;f=obj(data.get('native_filter') or {});pattern=str(f.get('pattern',d._options.get('key_prefix','')+'*'));cur=int(data.get('cursor') or 0);n=limit(data)
        def read():
            next_cursor,keys=d._client.scan(cur,match=pattern,count=n);keys=keys[:500];p=d._client.pipeline(transaction=False)
            for k in keys:p.type(k);p.ttl(k)
            values=p.execute();rows=[dict(key=d._decode(k),type=d._decode(values[i*2]),ttl=values[i*2+1]) for i,k in enumerate(keys)]
            return dict(rows=rows,identities=[{'id':x['key']} for x in rows],next_cursor=str(next_cursor) if next_cursor else None,note='SCAN count is a hint; this page may contain more or fewer keys.')
        return await asyncio.to_thread(read)
    async def record(self,r,identity):
        d=self.driver;k=identity.get('id')
        def read():
            c=d._client;typ=d._decode(c.type(k));ttl=c.ttl(k);truncated=False
            if typ=='none':return None
            if typ=='string':truncated=c.strlen(k)>256000;value=c.getrange(k,0,255999)
            elif typ=='hash':
                truncated=c.hlen(k)>200
                if truncated: _,value=c.hscan(k,count=200)
                else:value=c.hgetall(k)
            elif typ=='list':truncated=c.llen(k)>200;value=c.lrange(k,0,199)
            elif typ=='set':
                truncated=c.scard(k)>200
                if truncated: _,value=c.sscan(k,count=200)
                else:value=c.smembers(k)
            elif typ=='zset':truncated=c.zcard(k)>200;value=c.zrange(k,0,199,withscores=True)
            elif typ=='stream':truncated=True;value=c.xrange(k,count=200)
            else:truncated=True;value='This module type is inspected through Query.'
            value=d._decode(value)
            if len(json.dumps(value))>256000:truncated=True;value='Value exceeds the 256 KB inspector limit.'
            return dict(key=k,type=typ,value=value,ttl=ttl,truncated=truncated,version=digest(c.dump(k)) if not truncated else None)
        return await asyncio.to_thread(read)
    async def mutate(self,r,data,meta):
        d=self.driver;op=data['operation'];values=obj(data.get('values',{}));identity=data.get('identity') or {};key=identity.get('id') if op!='insert' else values.get('key')
        if not isinstance(key,str) or not key:raise DbHubError('A key is required.')
        def write():
            from redis.exceptions import WatchError
            with d._client.pipeline() as p:
                try:
                    p.watch(key);raw=p.dump(key)
                    if op=='insert' and raw is not None:raise DbHubError('This key already exists. Inspect it to update it.')
                    if op!='insert' and (raw is None or digest(raw)!=identity.get('version')):conflict()
                    typ=values.get('type');ttl=int(values.get('ttl',-1));value=values.get('value');previous_ttl=p.pttl(key)
                    if op!='delete':
                        if values.get('key')!=key:raise DbHubError('The key cannot be changed.')
                        if typ not in ('string','hash','list','set','zset'):raise DbHubError('This type can only be changed through Query.')
                        if typ=='string' and not isinstance(value,str):raise DbHubError('String values must be strings.')
                        if typ=='hash' and (not isinstance(value,dict) or not value):raise DbHubError('Enter a non-empty object for a hash.')
                        if typ in ('list','set','zset') and (not isinstance(value,list) or not value):raise DbHubError('Enter a non-empty array; sorted sets use [member, score] pairs.')
                        if ttl < -1:raise DbHubError('TTL must be -1 (persistent) or seconds >= 0.')
                    p.multi();p.delete(key)
                    if op!='delete':
                        if typ=='string':p.set(key,value)
                        elif typ=='hash':p.hset(key,mapping=value)
                        elif typ=='list':p.rpush(key,*value)
                        elif typ=='set':p.sadd(key,*value)
                        else:p.zadd(key,{str(x[0]):float(x[1]) for x in value})
                        if data.get('preserve_ttl',op!='insert') and previous_ttl>=0:p.pexpire(key,previous_ttl)
                        elif ttl>=0:p.expire(key,ttl)
                    p.execute();return dict(affected=1,rows=[])
                except WatchError:conflict()
        return await asyncio.to_thread(write)

class Graph:
    def __init__(self,b):self.driver=getattr(b.driver,'_inner',b.driver)
    async def query(self,s,p=None,ro=True):return await self.driver.query(s,p or {},ro)
    async def catalog(self):
        raw=await self.driver.list_schema();labels=raw.get('labels',[]);rels=raw.get('relationship_types',[])
        return dict(resources=[dict(name=label,parts=[label],group='Labels',label=label,kind='label') for label in labels]+[dict(name='relationship:'+x,parts=[x],group='Relationship types',label=x,kind='relationship') for x in rels],family='graph',visual=True,note='' if labels else 'This transport did not expose labels. Use the Cypher query editor.')
    async def metadata(self,r):
        relationship=r['kind']=='relationship';raw=await self.query('CALL db.schema.relTypeProperties()' if relationship else 'CALL db.schema.nodeTypeProperties()');fields=[]
        for x in raw.get('rows',[]):
            match=x.get('relType','').strip(':`')==r['parts'][0] if relationship else r['parts'][0] in x.get('nodeLabels',[])
            if match and x.get('propertyName'):fields.append(dict(name=x['propertyName'],type=' / '.join(x.get('propertyTypes',[])),nullable=not x.get('mandatory')))
        indexes=(await self.query('SHOW INDEXES')).get('rows',[]);constraints=(await self.query('SHOW CONSTRAINTS')).get('rows',[])
        native=hasattr(self.driver,'_session')
        return dict(resource=r['name'],columns=fields,primary_key=['elementId'],foreign_keys=[],indexes=indexes,constraints=constraints,capabilities=caps(insert=native,update=native,delete=native,visual=True,note='Graph properties are reported by the server. Node deletion rejects nodes that still have relationships. HTTPS transports use the reviewed Cypher workflow.'))
    def pattern(self,r):
        label='`'+r['parts'][0].replace('`','``')+'`'
        return '(a)-[n:'+label+']->(b)' if r['kind']=='relationship' else '(n:'+label+')'
    async def page(self,r,data,meta):
        params={'limit':limit(data)+1,'offset':offset(data)};clauses=[];flt=obj(data.get('native_filter') or {})
        for i,(k,v) in enumerate(flt.items()):params['v'+str(i)]=v;clauses.append('n.`'+k.replace('`','``')+'`=$v'+str(i))
        sort=data.get('sort') or {};order='elementId(n)'
        if sort.get('column'):
            if sort['column'] not in {c['name'] for c in meta['columns']}:raise DbHubError('Unknown sort property.')
            order='n.`'+sort['column'].replace('`','``')+'`'+(' DESC' if sort.get('direction')=='desc' else ' ASC')+',elementId(n)'
        result=await self.query('MATCH '+self.pattern(r)+(' WHERE '+' AND '.join(clauses) if clauses else '')+' RETURN elementId(n) AS id,properties(n) AS properties'+(',elementId(a) AS source,elementId(b) AS target' if r['kind']=='relationship' else '')+' ORDER BY '+order+' SKIP $offset LIMIT $limit',params)
        rows=result.get('rows',[]);n=limit(data);return dict(rows=rows[:n],identities=[dict(id=x['id'],original=x['properties']) for x in rows[:n]],next_offset=offset(data)+n if len(rows)>n else None)
    async def record(self,r,identity):
        result=await self.query('MATCH '+self.pattern(r)+' WHERE elementId(n)=$id RETURN elementId(n) AS id,properties(n) AS properties',{'id':identity['id']});return next(iter(result['rows']),None)
    async def mutate(self,r,data,meta):
        vals=obj(data.get('values',{}));props=obj(vals.get('properties',{}));identity=data.get('identity') or {};op=data['operation'];params=dict(props=props,id=identity.get('id'),original=identity.get('original'))
        if op=='insert':
            if r['kind']=='relationship':
                params.update(source=vals.get('source'),target=vals.get('target'));statement='MATCH (a),(b) WHERE elementId(a)=$source AND elementId(b)=$target CREATE '+self.pattern(r)+' SET n=$props RETURN elementId(n) AS id'
            else:statement='CREATE '+self.pattern(r)+' SET n=$props RETURN elementId(n) AS id'
        else:
            statement='MATCH '+self.pattern(r)+' WHERE elementId(n)=$id AND properties(n)=$original '+('DELETE n RETURN $id AS id' if op=='delete' else 'SET n=$props RETURN elementId(n) AS id')
        result=await self.query(statement,params,False)
        if len(result.get('rows',[]))!=1:conflict()
        return dict(affected=1,rows=result['rows'])
    async def diagram(self,resources,mode='topology'):
        if mode != 'sample':
            result=await self.query('CALL db.schema.visualization()')
            raw=next(iter(result.get('rows',[])),{});nodes=raw.get('nodes',[]);rels=raw.get('relationships',[]);names={n['_id']:str(n.get('name') or '/'.join(n.get('_labels',[]))) for n in nodes}
            props=(await self.query('CALL db.schema.nodeTypeProperties()')).get('rows',[])
            tables=[dict(resource=names[n['_id']],columns=[dict(name=p['propertyName'],type=' / '.join(p.get('propertyTypes',[]))) for p in props if p.get('propertyName') and names[n['_id']] in p.get('nodeLabels',[])],primary_key=[],foreign_keys=[]) for n in nodes[:120]]
            edges=[dict(source=names[x['_start']],target=names[x['_end']],name=x.get('_type','relationship'),columns=[],target_columns=[]) for x in rels[:500] if x.get('_start') in names and x.get('_end') in names]
            return dict(tables=tables,edges=edges,note='Server-reported label topology. Switch to Sample data for up to 100 stored relationships.')
        result=await self.query('MATCH (a)-[r]->(b) RETURN elementId(a) AS source,labels(a) AS source_labels,type(r) AS relationship,elementId(b) AS target,labels(b) AS target_labels LIMIT 100')
        nodes={};edges=[]
        for row in result.get('rows',[]):
            for side in ('source','target'):nodes[row[side]]=dict(resource=row[side],columns=[dict(name=x,type='label') for x in row[side+'_labels']],primary_key=[],foreign_keys=[])
            edges.append(dict(source=row['source'],target=row['target'],name=row['relationship'],columns=[],target_columns=[],sampled=True))
        return dict(tables=list(nodes.values()),edges=edges,sampled=True,note='Bounded sample: up to 100 stored relationships. These are data relationships, not relational foreign-key constraints.')

class Functions:
    def __init__(self,b):self.driver=b.driver;self.kind=b.conn_cfg['driver']
    async def catalog(self):
        from usr.plugins.db_hub.helpers.workspace import resources
        raw=await self.driver.list_schema();items=resources(raw)
        for r in items:r.update(group='Functions' if self.kind=='convex' else 'Ledgers',label=r['name'],kind='function')
        if not items:
            opts=getattr(self.driver,'_options',{});items=[dict(name=x,parts=[x],label=x,group='Configured resources',kind='function') for x in opts.get('explorer_resources',[]) if isinstance(x,str)]
        return dict(resources=items,family='function',visual=False,configuration=raw,note='Use the native Query editor. Resources are only shown when the API or connection options.explorer_resources exposes them.')
    async def metadata(self,r):return dict(resource=r['name'],columns=[],primary_key=[],foreign_keys=[],configuration=await self.driver.list_schema(r['name']),capabilities=caps(page=None,filter=False,sort=False,note='This connection exposes functions or ledger operations. Use Query with native parameters; direct table CRUD is unavailable.'))
    async def page(self,r,data,meta):return dict(rows=[],note=meta['capabilities']['note'])

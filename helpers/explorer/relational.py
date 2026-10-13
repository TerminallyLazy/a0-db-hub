"""Dialect-aware relational explorers. Unenforced keys never authorize row edits."""
import asyncio
import datetime
import decimal
from .common import *

class SQLAlchemy:
    def __init__(self,bound): self.driver=bound.driver; self.engine=self.driver._engine
    async def catalog(self):
        def read():
            import sqlalchemy as sa
            with self.engine.connect() as c:
                i=sa.inspect(c); resources=[]
                for schema in i.get_schema_names():
                    if schema in {'information_schema','pg_catalog'}: continue
                    for kind, names in [('table',i.get_table_names(schema)),('view',i.get_view_names(schema))]:
                        for name in names:
                            resources.append(dict(name=schema+'.'+name,parts=[schema,name],group=schema,label=name,kind=kind))
                            if len(resources)>1000: return dict(resources=resources[:1000],truncated=True,family='relational',visual=True)
                return dict(resources=resources,family='relational',visual=True)
        return await asyncio.to_thread(read)
    def table(self,c,r):
        import sqlalchemy as sa
        return sa.Table(r['parts'][-1],sa.MetaData(),schema=r['parts'][0],autoload_with=c)
    async def metadata(self,r):
        def read():
            import sqlalchemy as sa
            with self.engine.connect() as c:
                i=sa.inspect(c);schema,name=r['parts']; cols=i.get_columns(name,schema);pk=i.get_pk_constraint(name,schema).get('constrained_columns',[])
                foreign=[dict(name=f.get('name'),columns=f['constrained_columns'],target=(f.get('referred_schema') or schema)+'.'+f['referred_table'],target_columns=f['referred_columns'],enforced=True,**f.get('options',{})) for f in i.get_foreign_keys(name,schema)]
                editable=bool(pk) and r['kind']=='table'
                return dict(resource=r['name'],columns=[dict(name=x['name'],type=str(x['type']),nullable=x.get('nullable'),default=str(x.get('default')) if x.get('default') is not None else None,generated=bool(x.get('computed') or x.get('identity',{}).get('always'))) for x in cols],primary_key=pk,foreign_keys=foreign,indexes=plain(i.get_indexes(name,schema)),constraints=plain(i.get_unique_constraints(name,schema)),capabilities=caps(insert=editable,update=editable,delete=editable,visual=True,note='' if editable else 'Views and tables without a primary key are browse-only.'))
        return await asyncio.to_thread(read)
    def typed(self,col,value):
        if value is None: return None
        try:
            typ=col.type.python_type
            if typ in (datetime.datetime,datetime.date,datetime.time) and isinstance(value,str): return typ.fromisoformat(value)
            if typ in (int,float,decimal.Decimal,uuid.UUID): return typ(value)
            return value
        except (NotImplementedError,ValueError,TypeError): return value
    def predicate(self,t,data,meta):
        expr=[]
        for f in filters(data,meta):
            c=t.c[f['column']];v=self.typed(c,f.get('value'));op=f['op']
            expr.append({'eq':lambda:c==v,'ne':lambda:c!=v,'lt':lambda:c<v,'lte':lambda:c<=v,'gt':lambda:c>v,'gte':lambda:c>=v,'contains':lambda:c.contains(str(v),autoescape=True),'null':lambda:c.is_(None),'notnull':lambda:c.is_not(None)}[op]())
        return expr
    async def page(self,r,data,meta):
        def read():
            import sqlalchemy as sa
            with self.engine.connect() as c:
                t=self.table(c,r); query=sa.select(t).where(*self.predicate(t,data,meta));sort=data.get('sort') or {}; order=[]
                if sort.get('column'):
                    if sort['column'] not in t.c: raise DbHubError('Unknown sort column.')
                    col=t.c[sort['column']];order.append(col.desc() if sort.get('direction')=='desc' else col.asc())
                order.extend(t.c[k] for k in meta['primary_key'] if k!=sort.get('column'))
                n=limit(data); off=offset(data);rows=[dict(x) for x in c.execute(query.order_by(*order).limit(n+1).offset(off)).mappings()]
                return dict(rows=rows[:n],identities=[dict(key={k:x[k] for k in meta['primary_key']},original=x) if meta['primary_key'] else None for x in rows[:n]],next_offset=off+n if len(rows)>n else None)
        return await asyncio.to_thread(read)
    async def mutate(self,r,data,meta):
        values=checked_columns(meta,data.get('values',{}),data['operation'])
        def write():
            import sqlalchemy as sa
            with self.engine.begin() as c:
                t=self.table(c,r);vals={k:self.typed(t.c[k],v) for k,v in values.items()};op=data['operation']
                if op=='insert': result=c.execute(t.insert().values(**vals))
                else:
                    original=obj((data.get('identity') or {}).get('original'));key=(data.get('identity') or {}).get('key',{})
                    if set(original)!=set(t.c.keys()) or set(key)!=set(meta['primary_key']) or any(original[k]!=key[k] for k in key): raise DbHubError('Refresh this record to retrieve its full identity.')
                    pred=[t.c[k].is_(None) if v is None else t.c[k]==self.typed(t.c[k],v) for k,v in original.items()]
                    stmt=t.delete().where(*pred) if op=='delete' else t.update().where(*pred).values(**vals)
                    result=c.execute(stmt)
                    if result.rowcount!=1: conflict()
                return dict(affected=result.rowcount,rows=[])
        return await asyncio.to_thread(write)

class Warehouse:
    """DuckDB with enforced PKs; Snowflake and BigQuery never assume unique keys."""
    def __init__(self,bound): self.bound=bound;self.driver=bound.driver;self.kind=bound.conn_cfg['driver']
    async def catalog(self):
        from usr.plugins.db_hub.helpers.workspace import resources
        raw=await self.driver.list_schema();items=resources(raw)
        for r in items: r.update(group='.'.join(r['parts'][:-1]) or 'Tables',label=r['parts'][-1],kind='table')
        return dict(resources=items[:1000],truncated=len(items)>1000,family='relational',visual=True)
    def ident(self,r):
        if self.kind=='bigquery':
            parts=[self.driver._project]+r['parts']
            if any('`' in p for p in parts): raise DbHubError('Unsupported identifier.')
            return '`'+'.'.join(parts)+'`'
        return '.'.join(quote(p) for p in r['parts'])
    async def metadata(self,r):
        pk=[];foreign=[];constraints=[];indexes=[]
        if self.kind=='duckdb':
            def read():
                schema,name=r['parts'];c=self.driver._conn
                cols=c.execute('SELECT column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position',[schema,name]).fetchall()
                cons=c.execute('SELECT constraint_type,constraint_column_names,referenced_table,referenced_column_names,constraint_text FROM duckdb_constraints() WHERE schema_name=? AND table_name=?',[schema,name]).fetchall()
                return cols,cons
            cols,cons=await asyncio.to_thread(read)
            columns=[dict(name=x[0],type=x[1],nullable=x[2]=='YES',default=x[3]) for x in cols]
            for c in cons:
                constraints.append(dict(type=c[0],columns=c[1],definition=c[4]))
                if c[0]=='PRIMARY KEY':pk=c[1]
                if c[0]=='FOREIGN KEY':foreign.append(dict(name=c[4],columns=c[1],target=r['parts'][0]+'.'+c[2],target_columns=c[3],enforced=True))
        elif self.kind=='bigquery':
            info=await asyncio.to_thread(self.driver._client.get_table,self.driver._project+'.'+r['name']);api=info.to_api_repr();tc=api.get('tableConstraints',{})
            columns=[dict(name=f.name,type=f.field_type,nullable=f.mode!='REQUIRED',mode=f.mode,fields=[x.to_api_repr() for x in f.fields]) for f in info.schema];pk=tc.get('primaryKey',{}).get('columns',[])
            for f in tc.get('foreignKeys',[]):
                ref=f['referencedTable'];foreign.append(dict(name=f.get('name'),columns=[x['referencingColumn'] for x in f['columnReferences']],target=ref['datasetId']+'.'+ref['tableId'],target_columns=[x['referencedColumn'] for x in f['columnReferences']],enforced=False))
            constraints=tc;indexes=api.get('clustering',{});r['kind']='table' if info.table_type=='TABLE' else 'view'
        else:
            raw=await self.driver.list_schema(r['name']);columns=raw.get('columns',[])
            def read():
                conn=self.driver._connection()
                try:
                    cur=conn.cursor();details=[]
                    for command in ('SHOW PRIMARY KEYS IN TABLE ','SHOW IMPORTED KEYS IN TABLE '):
                        cur.execute(command+self.ident(r));names=[x[0] for x in cur.description];details.append([dict(zip(names,row)) for row in cur.fetchall()])
                    return details
                finally:conn.close()
            keys,refs=await asyncio.to_thread(read);pk=[x['column_name'] for x in sorted(keys,key=lambda x:x['key_sequence'])];groups={}
            for x in refs:groups.setdefault(x['fk_name'],[]).append(x)
            for name,group in groups.items():
                group.sort(key=lambda x:x['key_sequence']);foreign.append(dict(name=name,columns=[x['fk_column_name'] for x in group],target=group[0]['pk_schema_name']+'.'+group[0]['pk_table_name'],target_columns=[x['pk_column_name'] for x in group],enforced=False))
            constraints=keys+refs
        editable=self.kind=='duckdb' and bool(pk) and r['kind']=='table'
        return dict(resource=r['name'],columns=columns,primary_key=pk,foreign_keys=foreign,indexes=indexes,constraints=constraints,capabilities=caps(insert=r['kind']=='table',update=editable,delete=editable,visual=True,note='' if editable else 'Record updates and deletes require an enforced unique identity. Use reviewed Query DML when keys are absent or unenforced.'))
    async def sql(self,statement,params):
        if self.kind=='duckdb':
            def run():
                c=self.driver._conn.execute(statement,params);cols=[x[0] for x in c.description];return dict(rows=[dict(zip(cols,x)) for x in c.fetchall()])
            return await asyncio.to_thread(run)
        return await self.driver.query(statement,params,True)
    def placeholder(self,k): return {'duckdb':'$'+k,'snowflake':'%('+k+')s','bigquery':'@'+k}[self.kind]
    async def page(self,r,data,meta):
        params={};where=[]
        for i,f in enumerate(filters(data,meta)):
            col=('`'+f['column'].replace('`','')+'`') if self.kind=='bigquery' else quote(f['column']);op=f['op'];k='v'+str(i)
            if op in ('null','notnull') or (f.get('value') is None and op in ('eq','ne')):where.append(col+(' IS NULL' if op in ('null','eq') else ' IS NOT NULL'));continue
            value=f.get('value');ph=self.placeholder(k)
            if op=='contains':where.append(f'CAST({col} AS '+('STRING' if self.kind=='bigquery' else 'VARCHAR')+f') LIKE {ph}');params[k]='%'+str(value)+'%'
            else:
                operator={'eq':'=','ne':'<>','lt':'<','lte':'<=','gt':'>','gte':'>='}[op];where.append(f'{col} {operator} {ph}');params[k]=value
        order=[];sort=data.get('sort') or {};names={c['name'] for c in meta['columns']}
        if sort.get('column'):
            if sort['column'] not in names:raise DbHubError('Unknown sort column.')
            order.append(sort['column'])
        order.extend(x for x in meta['primary_key'] if x not in order)
        q=lambda x:'`'+x.replace('`','')+'`' if self.kind=='bigquery' else quote(x)
        n=limit(data);off=offset(data);statement='SELECT * FROM '+self.ident(r)+(' WHERE '+' AND '.join(where) if where else '')+(' ORDER BY '+','.join(q(k)+(' DESC' if k==sort.get('column') and sort.get('direction')=='desc' else ' ASC') for k in order) if order else '')+f' LIMIT {n+1} OFFSET {off}'
        result=await self.sql(statement,params);rows=result['rows'];return dict(rows=rows[:n],identities=[dict(key={k:x[k] for k in meta['primary_key']},original=x) for x in rows[:n]],next_offset=off+n if len(rows)>n else None)
    async def mutate(self,r,data,meta):
        vals=checked_columns(meta,data.get('values',{}),data['operation']);op=data['operation'];params={};q=lambda x:'`'+x.replace('`','')+'`' if self.kind=='bigquery' else quote(x)
        for i,(k,v) in enumerate(vals.items()):params['v'+str(i)]=v
        if op=='insert':
            if not vals:raise DbHubError('Enter at least one column.')
            statement=f'INSERT INTO {self.ident(r)} ('+','.join(q(k) for k in vals)+') VALUES ('+','.join(self.placeholder(k) for k in params)+')'
        else:
            original=obj((data.get('identity') or {}).get('original'));key=(data.get('identity') or {}).get('key',{})
            if set(original)!={c['name'] for c in meta['columns']} or set(key)!=set(meta['primary_key']):raise DbHubError('Refresh this record before editing.')
            where=[]
            for i,(k,v) in enumerate(original.items()):params['o'+str(i)]=v;where.append(q(k)+' IS NOT DISTINCT FROM '+self.placeholder('o'+str(i)))
            statement=('DELETE FROM '+self.ident(r) if op=='delete' else 'UPDATE '+self.ident(r)+' SET '+','.join(q(k)+'='+self.placeholder('v'+str(i)) for i,k in enumerate(vals)))+' WHERE '+' AND '.join(where)+' RETURNING *'
        if self.kind=='duckdb':
            def run():
                c=self.driver._conn;c.execute('BEGIN TRANSACTION')
                try:
                    rows=c.execute(statement,params).fetchall()
                    if op!='insert' and len(rows)!=1:conflict()
                    c.execute('COMMIT');return dict(affected=1,rows=[])
                except BaseException:c.execute('ROLLBACK');raise
            return await asyncio.to_thread(run)
        result=await self.driver.query(statement,params,False);return dict(affected=result.get('rowcount'),rows=result.get('rows',[]))

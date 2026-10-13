"""PostgreSQL catalog discovery and primary-key editing with atomic xmin checks."""
import json
from .common import *

CATALOG = """
SELECT n.nspname AS schema, c.relname AS name, c.relkind::text AS kind,
       c.oid::bigint AS oid, has_table_privilege(c.oid, 'SELECT') AS readable,
       has_table_privilege(c.oid, 'INSERT') AS can_insert,
       has_table_privilege(c.oid, 'UPDATE') AS can_update,
       has_table_privilege(c.oid, 'DELETE') AS can_delete
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p','v','m','f')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
  AND n.nspname NOT LIKE 'pg_toast%%'
  AND has_schema_privilege(n.oid,'USAGE')
ORDER BY n.nspname,c.relname LIMIT 1001
"""

class Postgres:
    def __init__(self, bound): self.bound = bound; self.driver = bound.driver
    async def catalog(self):
        async with self.driver._pool.acquire() as c:
            rows = await c.fetch(CATALOG)
        return {'resources': [dict(name=r['schema']+'.'+r['name'], parts=[r['schema'],r['name']], group=r['schema'], label=r['name'], kind='view' if r['kind'] in ('v','m') else 'table', readable=r['readable']) for r in rows[:1000]], 'truncated':len(rows)>1000, 'family':'relational', 'visual': True}

    async def metadata(self, resource):
        parts = resource['parts']; ident = '.'.join(quote(p) for p in parts)
        async with self.driver._pool.acquire() as c:
            row = await c.fetchrow('SELECT c.oid::bigint AS oid,c.relkind::text AS kind,has_table_privilege(c.oid,\'INSERT\') AS ins,has_table_privilege(c.oid,\'UPDATE\') AS upd,has_table_privilege(c.oid,\'DELETE\') AS del FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname=$2', *parts)
            if not row: raise DbHubError('The selected table no longer exists.')
            columns = await c.fetch("""SELECT a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type,
                NOT a.attnotnull AS nullable,pg_get_expr(d.adbin,d.adrelid) AS default,
                a.attgenerated <> '' OR a.attidentity='a' AS generated
                FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
                WHERE a.attrelid=$1::oid AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""",row['oid'])
            constraints = await c.fetch("""SELECT con.conname AS name,con.contype::text AS type,con.convalidated AS validated,
                pg_get_constraintdef(con.oid) AS definition,
                ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.num ORDER BY k.ord) AS columns,
                rn.nspname AS ref_schema,rc.relname AS ref_table,
                ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(num,ord) JOIN pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.num ORDER BY k.ord) AS ref_columns,
                con.confupdtype::text AS update_rule,con.confdeltype::text AS delete_rule
                FROM pg_constraint con LEFT JOIN pg_class rc ON rc.oid=con.confrelid
                LEFT JOIN pg_namespace rn ON rn.oid=rc.relnamespace WHERE con.conrelid=$1::oid ORDER BY con.conname""",row['oid'])
            indexes = await c.fetch('SELECT indexname AS name,indexdef AS definition FROM pg_indexes WHERE schemaname=$1 AND tablename=$2',*parts)
        constraints=[dict(r) for r in constraints]
        pk = next((r['columns'] for r in constraints if r['type']=='p'),[])
        rules={'a':'NO ACTION','r':'RESTRICT','c':'CASCADE','n':'SET NULL','d':'SET DEFAULT'}
        foreign=[dict(name=r['name'], columns=r['columns'], target=r['ref_schema']+'.'+r['ref_table'], target_parts=[r['ref_schema'],r['ref_table']], target_columns=r['ref_columns'], enforced=True, validated=r['validated'], update_rule=rules.get(r['update_rule']),delete_rule=rules.get(r['delete_rule'])) for r in constraints if r['type']=='f']
        editable=row['kind'] in ('r','p') and bool(pk)
        return dict(resource=resource['name'],columns=[dict(r) for r in columns], primary_key=pk,foreign_keys=foreign,indexes=[dict(r) for r in indexes],constraints=constraints,
            capabilities=caps(insert=editable and row['ins'],update=editable and row['upd'],delete=editable and row['del'],visual=True,note='' if editable else 'Views, foreign tables and tables without a primary key are browse-only. Use Query for deliberate changes.'))

    def identifier(self,r): return '.'.join(quote(p) for p in r['parts'])
    async def page(self, r, data, meta):
        name=self.identifier(r); params=[]; where=[]
        for f in filters(data, meta):
            col='t.'+quote(f['column']); op=f['op']
            if op in ('null','notnull'): where.append(col+(' IS NULL' if op=='null' else ' IS NOT NULL'));continue
            if op=='contains':
                params.append('%'+str(f.get('value','')).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%');where.append(f'{col}::text ILIKE ${len(params)}');continue
            params.append(json.dumps({f['column']:f.get('value')})); value=f'(jsonb_populate_record(NULL::{name}, ${len(params)}::jsonb)).{quote(f["column"])}'
            operator={'eq':'IS NOT DISTINCT FROM','ne':'IS DISTINCT FROM','lt':'<','lte':'<=','gt':'>','gte':'>='}[op]
            where.append(f'{col} {operator} {value}')
        sort=data.get('sort') or {}; order=[]; names={x['name'] for x in meta['columns']}
        if sort.get('column'):
            if sort['column'] not in names: raise DbHubError('Unknown sort column.')
            order.append('t.'+quote(sort['column'])+(' DESC' if sort.get('direction')=='desc' else ' ASC'))
        order.extend('t.'+quote(k) for k in meta['primary_key'] if k!=sort.get('column'))
        n=limit(data); off=offset(data); version='t.xmin::text' if r['kind']=='table' and meta['primary_key'] else 'NULL::text'
        sql=f'SELECT to_jsonb(t)::text AS record, {version} AS version FROM {name} t'+(' WHERE '+' AND '.join(where) if where else '')+(' ORDER BY '+','.join(order) if order else '')+f' LIMIT {n+1} OFFSET {off}'
        async with self.driver._pool.acquire() as c:
            async with c.transaction(readonly=True): rows=await c.fetch(sql,*params)
        records=[json.loads(x['record']) for x in rows[:n]]
        identities=[dict(key={k:rec[k] for k in meta['primary_key']},version=raw['version']) if meta['primary_key'] else None for rec,raw in zip(records,rows)]
        return dict(rows=records,identities=identities,next_offset=off+n if len(rows)>n else None,note='' if order else 'No primary key: pages may change when concurrent writes occur.')

    async def mutate(self,r,data,meta):
        op=data['operation']; values=checked_columns(meta,data.get('values',{}),op); table=self.identifier(r)
        identity=data.get('identity') or {}; pk=meta['primary_key']
        if op!='insert' and (set(identity.get('key',{}))!=set(pk) or not identity.get('version')): raise DbHubError('A current primary key and record version are required.')
        async with self.driver._pool.acquire() as c:
            async with c.transaction():
                if op=='insert':
                    if values:
                        cols=','.join(quote(k) for k in values)
                        sql=f'INSERT INTO {table} ({cols}) SELECT {cols} FROM jsonb_populate_record(NULL::{table},$1::jsonb) RETURNING to_jsonb({quote(r["parts"][-1])})::text'
                        result=await c.fetchval(sql,json.dumps(values))
                    else: result=await c.fetchval(f'INSERT INTO {table} DEFAULT VALUES RETURNING to_jsonb({quote(r["parts"][-1])})::text')
                else:
                    where=' AND '.join(f't.{quote(k)} IS NOT DISTINCT FROM (jsonb_populate_record(NULL::{table},$1::jsonb)).{quote(k)}' for k in pk)+' AND t.xmin::text=$2'
                    args=[json.dumps(identity['key']),str(identity['version'])]
                    if op=='update':
                        if not values: raise DbHubError('No changed columns to apply.')
                        assignments=','.join(f'{quote(k)}=(jsonb_populate_record(NULL::{table},$3::jsonb)).{quote(k)}' for k in values);args.append(json.dumps(values))
                        sql=f'UPDATE {table} t SET {assignments} WHERE {where} RETURNING to_jsonb(t)::text'
                    else: sql=f'DELETE FROM {table} t WHERE {where} RETURNING to_jsonb(t)::text'
                    result=await c.fetchval(sql,*args)
                    if result is None: conflict()
        return dict(affected=1,rows=[json.loads(result)])

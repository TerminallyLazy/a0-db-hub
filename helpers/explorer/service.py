"""Authenticated explorer dispatch, bounded metadata and exact mutation review."""
import asyncio
import json
import secrets
import threading
import time
from usr.plugins.db_hub.helpers import registry, workspace
from usr.plugins.db_hub.helpers.base import DbHubError, ReadOnlyError
from .common import plain, digest, obj
from . import table_setup
from .postgres import Postgres
from .relational import SQLAlchemy, Warehouse
from .vectors import Vectors
from .native import Mongo, Redis, Graph, Functions
from .spacetimedb import SpacetimeDB

_REVIEWS = {}
_REVIEW_LOCK = threading.Lock()

def adapter(bound):
    kind=bound.conn_cfg['driver']
    if kind == 'spacetimedb':
        return SpacetimeDB(bound)
    cls=Postgres if kind=='postgres' else SQLAlchemy if kind=='sqlalchemy' else Warehouse if kind in ('duckdb','snowflake','bigquery') else Mongo if kind=='mongodb' else Redis if kind=='redis' else Graph if kind in ('neo4j','hydradb') else Functions if kind in ('convex','fluree') else Vectors
    return cls(bound)

def review(owner,payload):
    now=time.monotonic()
    with _REVIEW_LOCK:
        for key,value in list(_REVIEWS.items()):
            if value[0]<now:del _REVIEWS[key]
        if len(_REVIEWS)>=500:raise DbHubError('Too many pending changes. Wait a few minutes and retry.')
        token=secrets.token_urlsafe(32);_REVIEWS[token]=(now+300,owner,json.loads(json.dumps(payload)))
    return token

def consume(owner,token):
    with _REVIEW_LOCK:
        item=_REVIEWS.get(token)
        if not item or item[0]<time.monotonic() or item[1]!=owner:raise DbHubError('The review expired or belongs to another session. Review the change again.')
        del _REVIEWS[token]
    return item[2]

async def execute(data,project='',profile='',owner=''):
    if 'db_hub' not in workspace.enabled_plugins(project,profile):raise DbHubError('Enable Database Hub in this scope.')
    action=data.get('action','catalog');scope=(owner,project,profile,str(data.get('connection') or ''))
    if action=='apply':data={**consume(scope,str(data.get('token') or '')),'action':'apply'}
    bound=await registry.get_connection(str(data.get('connection') or ''),project_name=project,agent_profile=profile)
    async def run():
        if action in ('table_options', 'review_table') or (action == 'apply' and data.get('operation') == 'create_table'):
            readonly = registry.effective_readonly(bound.conn_cfg, bound.glob, None)
            choices = await table_setup.options(bound)
            if action == 'table_options': return {**choices, 'readonly': readonly}
            if readonly: raise ReadOnlyError('This saved connection is read-only.')
            if not choices['supported']: raise DbHubError(choices['note'])
            spec = table_setup.normalize(data.get('definition'), choices)
            statement = table_setup.compile_sql(bound, spec)
            fingerprint = digest(registry._fingerprint(bound.conn_cfg))
            if action == 'review_table':
                payload = dict(connection=data['connection'], operation='create_table', definition=spec, statement=statement, fingerprint=fingerprint)
                return dict(token=review(scope, payload), statement=statement, definition=spec, connection=data['connection'], note=choices['note'], expires_in=300)
            if statement != data.get('statement') or fingerprint != data.get('fingerprint'):
                raise DbHubError('The connection or table definition changed. Review the table again.')
            await bound.query(statement, readonly=False)
            return dict(created=True, resource=spec['schema']+'.'+spec['name'])
        a=adapter(bound);catalog=await a.catalog()
        if action=='catalog':return catalog
        if action=='diagram':
            if isinstance(a,Graph):return await a.diagram(catalog['resources'],data.get('graph_mode','topology'))
            if not catalog.get('visual'):raise DbHubError('This adapter does not expose relational relationships.')
            names=data.get('targets') or [];schema=data.get('schema');chosen=[r for r in catalog['resources'] if (not names or r['name'] in names) and (not schema or r.get('group')==schema)]
            start=max(0,int(data.get('offset',0)));chunk=chosen[start:start+30];tables=[];errors=[]
            for r in chunk:
                try:tables.append(await a.metadata(r))
                except Exception as exc:errors.append(dict(resource=r['name'],reason=bound.mask(str(exc))))
            edges=[dict(source=t['resource'],**f) for t in tables for f in t.get('foreign_keys',[])]
            return dict(tables=tables,edges=edges,omitted=max(0,len(chosen)-start-30),next_offset=start+30 if len(chosen)>start+30 else None,inaccessible=errors)
        resource=next((r for r in catalog['resources'] if r['name']==data.get('target')),None)
        if not resource:raise DbHubError('Refresh resources and select a returned resource.')
        meta=await a.metadata(resource);readonly=registry.effective_readonly(bound.conn_cfg,bound.glob,None)
        if readonly:
            for op in ('insert','update','delete'):meta['capabilities'][op]=False
            meta['capabilities']['note']='This saved connection is read-only. '+meta['capabilities'].get('note','')
        if action=='metadata':return meta
        if action=='page':return await a.page(resource,data,meta)
        if action=='record':
            if not hasattr(a,'record'):return dict(record=data.get('row'),identity=data.get('identity'))
            identity=obj(data.get('identity') or {});record=await a.record(resource,identity)
            if record is None:raise DbHubError('The record no longer exists.')
            if isinstance(a,Mongo):identity['original']=record
            elif isinstance(a,Redis):identity['version']=record.pop('version',None)
            elif isinstance(a,Graph):identity['original']=record.get('properties',{})
            else:identity['version']=digest(record)
            return dict(record=record,identity=identity)
        if action in ('review','apply'):
            if readonly:raise ReadOnlyError('This saved connection is read-only.')
            op=data.get('operation')
            if op not in ('insert','update','delete') or not meta['capabilities'].get(op):raise DbHubError('This resource does not support that record operation. Use the reviewed Query workflow.')
            obj(data.get('values',{}))
            if action=='review':
                payload={key:data[key] for key in ('connection','target','operation','values','identity','preserve_ttl') if key in data}
                return dict(token=review(scope,payload),request=payload,note=meta['capabilities'].get('note',''),expires_in=300)
            return await a.mutate(resource,data,meta)
        raise DbHubError('Unknown explorer action.')
    started=time.monotonic()
    try:
        result=await asyncio.wait_for(run(),max(10,min(bound._timeout(),90)))
        result['elapsed_ms']=round((time.monotonic()-started)*1000,1)
        return json.loads(bound.mask(json.dumps(plain(result),default=str)))
    except asyncio.TimeoutError as exc:raise DbHubError('The explorer request timed out. A submitted write may still finish; refresh before retrying.') from exc
    except Exception as exc:raise DbHubError(bound.mask(str(exc))) from exc

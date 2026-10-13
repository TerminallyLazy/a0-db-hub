import asyncio
import secrets
import threading
from flask import session
from helpers.api import ApiHandler
from usr.plugins.db_hub.helpers import workspace, secrets_bridge, registry

# Flask requests can run on different event loops. Cancellation is sent back to
# the query's owning loop and is bound to the authenticated browser session.
_RUNNING = {}
_LOCK = threading.Lock()

class Workspace(ApiHandler):
    async def process(self,input,request):
        project=str(input.get('project_name') or '')
        profile=str(input.get('agent_profile') or '')
        owner=session.get('dbhub_query_owner')
        if not owner:owner=secrets.token_urlsafe(24);session['dbhub_query_owner']=owner
        request_id=str(input.get('request_id') or '')[:100]
        key=(owner,project,profile,str(input.get('connection') or ''),request_id)
        if input.get('action')=='cancel':
            with _LOCK:item=_RUNNING.get(key)
            if item:item[0].call_soon_threadsafe(item[1].cancel)
            return {'ok':True,'cancel_requested':bool(item)}
        try:
            cfg=registry.load_config(None,project_name=project,agent_profile=profile)
            driver=next((c['driver'] for c in cfg['connections'] if c['id']==input.get('connection')),None)
            cancellable=driver=='postgres' and input.get('action')=='query' and not input.get('allow_writes') and bool(request_id)
            task=asyncio.create_task(workspace.execute(input,project,profile))
            if cancellable:
                with _LOCK:_RUNNING[key]=(asyncio.get_running_loop(),task)
            try:return {'ok':True,**await task}
            except asyncio.CancelledError:
                return {'ok':True,'cancelled':True,'rows':[],'elapsed_ms':0,'note':'Read-only query cancelled.'}
            finally:
                if cancellable:
                    with _LOCK:_RUNNING.pop(key,None)
        except Exception as exc:
            return {'ok':False,'error':secrets_bridge.mask_text(str(exc),project_name=project)}

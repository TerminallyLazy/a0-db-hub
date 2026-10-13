from flask import session
import secrets
from helpers.api import ApiHandler
from usr.plugins.db_hub.helpers.explorer import service
from usr.plugins.db_hub.helpers import secrets_bridge

class Explorer(ApiHandler):
    async def process(self,input,request):
        project=str(input.get('project_name') or '');profile=str(input.get('agent_profile') or '')
        owner=session.get('dbhub_explorer_owner')
        if not owner:owner=secrets.token_urlsafe(24);session['dbhub_explorer_owner']=owner
        try:return {'ok':True,**await service.execute(input,project,profile,owner)}
        except Exception as exc:return {'ok':False,'error':secrets_bridge.mask_text(str(exc),project_name=project)}

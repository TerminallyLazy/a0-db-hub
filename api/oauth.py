import secrets
from flask import session
from helpers.api import ApiHandler
from usr.plugins.db_hub.helpers import oauth
from usr.plugins.db_hub.helpers.base import DbHubError

class OAuth(ApiHandler):
    async def process(self,input,request):
        owner=session.setdefault('db_hub_provider_owner',secrets.token_urlsafe(32))
        scope=(str(input.get('project_name') or ''),str(input.get('agent_profile') or ''))
        try:
            action=input.get('action')
            if action=='catalog': result={'providers':oauth.catalog()}
            elif action=='start': result=await oauth.start(owner,scope,request.headers.get('Origin') or request.host_url.rstrip('/'),input)
            elif action=='finish': result=await oauth.finish(owner,input)
            elif action=='status': result=await oauth.status(owner,input.get('handle'),scope)
            elif action=='discovery': result=await oauth.discovery(owner,input.get('handle'),scope)
            elif action=='import': result=oauth.import_draft(owner,input.get('handle'),scope,input)
            elif action=='disconnect': oauth.disconnect(owner,input.get('handle')); result={}
            else: raise DbHubError('Unknown OAuth action.')
            return {'ok':True,**result}
        except DbHubError as exc: return {'ok':False,'error':str(exc)}
        except Exception: return {'ok':False,'error':'OAuth setup could not complete. Check your registration and reconnect.'}

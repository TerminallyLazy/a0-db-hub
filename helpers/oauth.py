"""Registered-client OAuth with PKCE, browser-bound state, and secret-only tokens.

Database tokens are stored only when a user imports a draft. Account tokens for
Neon/Supabase feed temporary resource discovery. No authentication is implied by
merely opening an authorization URL.
"""
import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from usr.plugins.db_hub.helpers import extract, secrets_bridge, providers
from usr.plugins.db_hub.helpers.base import DbHubError

CALLBACK = '/plugins/db_hub/webui/oauth-callback.html'
_FLOWS = {}
_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()
PROVIDERS = {
 'bigquery': dict(name='Google BigQuery', auth='https://accounts.google.com/o/oauth2/v2/auth', token='https://oauth2.googleapis.com/token', scope='https://www.googleapis.com/auth/bigquery', method='post', docs='https://developers.google.com/identity/protocols/oauth2/web-server', note='Register a Google web application and enable the BigQuery API. Sign in grants BigQuery access; database roles still apply.'),
 'snowflake': dict(name='Snowflake', scope='session:role-any refresh_token', method='basic', docs='https://docs.snowflake.com/en/user-guide/oauth-custom', note='Create a Snowflake OAuth security integration with this callback URL. Enter your account URL and the integration client credentials.'),
 'convex': dict(name='Convex', auth='https://dashboard.convex.dev/oauth/authorize/project', token='https://api.convex.dev/oauth/token', scope='', method='post', docs='https://docs.convex.dev/platform-apis/oauth-applications', note='Register an OAuth app in Convex Team Settings. Choose the project in the consent screen, then supply its deployment URL.'),
 'supabase': dict(name='Supabase', auth='https://api.supabase.com/v1/oauth/authorize', token='https://api.supabase.com/v1/oauth/token', scope='', method='basic', docs='https://supabase.com/docs/guides/integrations/build-a-supabase-oauth-integration', note='Register an OAuth app with project and database read scopes. Sign in enables project discovery; an existing database password is still required.'),
 'neon': dict(name='Neon', auth='https://oauth2.neon.tech/oauth2/auth', token='https://oauth2.neon.tech/oauth2/token', scope='openid offline offline_access urn:neoncloud:projects:read urn:neoncloud:orgs:read', method='post', docs='https://neon.com/docs/guides/oauth-integration', note='Requires Neon partner OAuth credentials. Without a partner app, use the management-token or console flow above.'),
 'neo4j': dict(name='Neo4j OIDC', scope='openid offline_access', method='post', custom=True, docs='https://neo4j.com/docs/operations-manual/current/authentication-authorization/sso-integration/', note='Your Neo4j installation must trust this identity provider and accept its access tokens. Configure the matching client, endpoints, scopes and audience in your IdP.'),
 'mongodb': dict(name='MongoDB OIDC', scope='openid offline_access', method='post', custom=True, docs='https://www.mongodb.com/docs/atlas/security-oidc/', note='For clusters configured for OIDC workload identity. Use your trusted IdP client and access token audience. Atlas account OAuth alone does not authorize database queries.'),
 'weaviate': dict(name='Weaviate OIDC (self-hosted)', scope='openid offline_access', method='post', custom=True, docs='https://docs.weaviate.io/deploy/configuration/oidc', note='For a self-hosted cluster configured for OIDC. Weaviate Cloud recommends API keys; its old OIDC login is deprecated.'),
}


def catalog():
    return [dict(id=key,**{k:v for k,v in value.items() if k in {'name','docs','note','custom','scope'}}) for key,value in PROVIDERS.items()]


def _prune():
    for key in list(_FLOWS):
        if _FLOWS[key]['expires'] < time.monotonic(): del _FLOWS[key]


def _flow(owner, handle, scope=None):
    with _LOCK:
        _prune()
        state = _FLOWS.get(str(handle))
        if not state or state['owner'] != owner or (scope is not None and state['scope'] != scope):
            raise DbHubError('OAuth session expired or belongs to another browser/settings scope. Start again.')
        return state


def _https(url):
    parsed=urlsplit(str(url or ''))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise DbHubError('OAuth endpoints must be HTTPS URLs without credentials or fragments.')
    return parsed


def _public_endpoint(url):
    parsed=_https(url)
    try:
        addresses=socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError()
    except (OSError,ValueError):
        raise DbHubError('OAuth endpoints must resolve to public HTTPS servers.') from None


async def start(owner, scope, origin, data):
    provider=str(data.get('provider') or '')
    if provider not in PROVIDERS: raise DbHubError('Choose a supported OAuth provider.')
    spec=dict(PROVIDERS[provider])
    client_id=str(data.get('client_id') or '').strip()
    secret=str(data.get('client_secret') or '').strip()
    if not client_id or len(client_id)>2048: raise DbHubError('Enter the registered OAuth client ID.')
    if '§§secret(' in secret: secret=secrets_bridge.resolve_placeholders(secret,project_name=scope[0])
    if not secret and not spec.get('custom'): raise DbHubError('Enter the registered client secret or a Secret reference.')
    if provider == 'snowflake':
        account=str(data.get('account_url') or '').rstrip('/')
        parsed=_https(account)
        if not parsed.hostname.endswith('.snowflakecomputing.com') or parsed.path or parsed.query:
            raise DbHubError('Use your HTTPS Snowflake account URL (*.snowflakecomputing.com).')
        spec.update(auth=account+'/oauth/authorize',token=account+'/oauth/token-request')
    if spec.get('custom'):
        spec['auth']=str(data.get('authorization_url') or '')
        spec['token']=str(data.get('token_url') or '')
        await asyncio.to_thread(_public_endpoint,spec['auth'])
        await asyncio.to_thread(_public_endpoint,spec['token'])
        spec['scope']=str(data.get('scopes') or spec['scope'])
        spec['method']='basic' if data.get('token_auth') == 'basic' else 'post'
    origin_parts=urlsplit(origin)
    if origin_parts.scheme not in {'http','https'} or not origin_parts.netloc or origin_parts.username:
        raise DbHubError('Open Database Hub using its configured browser URL.')
    redirect=origin.rstrip('/')+CALLBACK
    handle=secrets.token_urlsafe(32); verifier=secrets.token_urlsafe(48)
    params=dict(client_id=client_id,redirect_uri=redirect,response_type='code',state=handle,
                code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),code_challenge_method='S256')
    if spec['scope']: params['scope']=spec['scope']
    if data.get('audience') and spec.get('custom'): params['audience']=str(data['audience'])
    if provider=='bigquery': params.update(access_type='offline',prompt='consent')
    with _LOCK:
        _prune()
        for key in list(_FLOWS):
            if _FLOWS[key]['owner']==owner: del _FLOWS[key]
        if len(_FLOWS)>=100: raise DbHubError('Too many OAuth sessions. Try later.')
        _FLOWS[handle]=dict(owner=owner,scope=scope,provider=provider,spec=spec,client_id=client_id,client_secret=secret,
                           redirect=redirect,verifier=verifier,expires=time.monotonic()+900,status='pending',result=None)
    return {'handle':handle,'authorization_url':spec['auth']+'?'+urlencode(params),'callback_url':redirect}


def _token_request(spec, client_id, client_secret, data):
    _public_endpoint(spec['token'])
    auth=None
    if spec['method']=='basic': auth=(client_id,client_secret)
    else:
        data={**data,'client_id':client_id}
        if client_secret: data['client_secret']=client_secret
    try:
        with httpx.Client(timeout=25,follow_redirects=False) as client:
            response=client.post(spec['token'],data=data,auth=auth)
        if response.status_code!=200:
            raise DbHubError(f'OAuth token exchange returned HTTP {response.status_code}. Check the app registration and callback URL.')
        tokens=response.json()
        if not isinstance(tokens.get('access_token'),str) or not tokens['access_token']:
            raise DbHubError('The OAuth provider did not return an access token.')
        return tokens
    except (httpx.HTTPError,ValueError):
        raise DbHubError('The OAuth token exchange could not complete. Start sign-in again.') from None


async def finish(owner, data):
    state=_flow(owner,data.get('state'))
    with _LOCK:
        if state['status']!='pending': raise DbHubError('This authorization response has already been used.')
        state['status']='exchanging'
    try:
        if data.get('error'): raise DbHubError('Provider authorization was declined. Start again when ready.')
        code=str(data.get('code') or '')
        if not code or len(code)>16384: raise DbHubError('Missing authorization code.')
        tokens=await asyncio.to_thread(_token_request,state['spec'],state['client_id'],state['client_secret'],
            dict(grant_type='authorization_code',code=code,redirect_uri=state['redirect'],code_verifier=state.pop('verifier')))
        state['tokens']=tokens
        state['tokens']['expires_at']=time.time()+float(tokens.get('expires_in',3600)) if tokens.get('expires_in') else 0
        state['status']='complete'
        return {'complete':True}
    except Exception:
        state['status']='failed'; state.pop('client_secret',None); state.pop('verifier',None)
        raise


async def status(owner,handle,scope):
    state=_flow(owner,handle,scope)
    if state['status']=='failed': raise DbHubError('OAuth sign-in failed. Start again and check provider settings.')
    return {'status':state['status'],'provider':state['provider']}


async def discovery(owner,handle,scope):
    state=_flow(owner,handle,scope)
    if state['status']!='complete': raise DbHubError('Finish provider sign-in first.')
    if state['provider'] not in {'neon','supabase'}: raise DbHubError('This provider connects directly to the resource you specify below.')
    return await providers.connect(owner,state['provider'],state['tokens']['access_token'],scope)


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode()


def import_draft(owner,handle,scope,data):
    state=_flow(owner,handle,scope)
    if state['status']!='complete': raise DbHubError('Finish provider sign-in first.')
    provider=state['provider']
    if provider in {'neon','supabase'}: raise DbHubError('Use the project dropdowns to import this account connection.')
    conn_id=str(data.get('connection_id') or provider)
    if not re.fullmatch('[a-z0-9_]+',conn_id): raise DbHubError('Use lowercase letters, numbers and underscores for the connection name.')
    dsn=str(data.get('dsn') or '').strip()
    parsed=urlsplit(dsn)
    schemes={'bigquery':{'bigquery'},'snowflake':{'snowflake'},'convex':{'https'},'neo4j':{'neo4j+s','bolt+s'},'mongodb':{'mongodb+srv','mongodb'},'weaviate':{'https'}}
    if parsed.scheme not in schemes[provider] or not parsed.hostname or parsed.password:
        raise DbHubError('Enter a valid resource connection URL without a password.')
    if provider=='convex' and not parsed.hostname.endswith('.convex.cloud'):
        raise DbHubError('Use the selected project deployment URL ending in .convex.cloud.')
    bundle={k:state[k] for k in ('provider','spec','client_id','client_secret','tokens')}
    key='DB_HUB_OAUTH_'+secrets.token_hex(12).upper()
    encoded = _encode(bundle)
    if len(encoded) > 131072: raise DbHubError('Provider credentials are too large to store.')
    manager = extract.get_default_secrets_manager()
    extract._write_secret(manager, extract._validate_new_key(key, manager), encoded)
    state['status']='imported'; state.pop('tokens',None); state.pop('client_secret',None)
    return {'connection':dict(id=conn_id,driver=provider,dsn=dsn,enabled=True,readonly=True,
        options={'oauth_credential':f'§§secret({key})'},description=PROVIDERS[provider]['name']+' OAuth'),
        'notice':'OAuth credentials saved to Secrets. Test and Save this read-only draft.'}


def resolve_options(driver, options, reference, project=''):
    """Called in a worker thread before each connection acquisition, including reuse."""
    with _REFRESH_LOCK:
        # Read again under the lock so a rotating refresh token cannot be reused.
        encoded=secrets_bridge.resolve_placeholders(reference,project_name=project)
        try: bundle=json.loads(base64.urlsafe_b64decode(encoded))
        except Exception: raise DbHubError('OAuth credentials are invalid. Connect again.') from None
        if bundle.get('provider')!=driver: raise DbHubError('OAuth credentials belong to a different database provider.')
        tokens=bundle['tokens']; expires=tokens.get('expires_at',0)
        if expires and expires<=time.time()+60:
            if not tokens.get('refresh_token'): raise DbHubError('OAuth access expired. Reconnect this database in settings.')
            fresh=_token_request(bundle['spec'],bundle['client_id'],bundle['client_secret'],
                                 {'grant_type':'refresh_token','refresh_token':tokens['refresh_token']})
            tokens.update(fresh); tokens['expires_at']=time.time()+float(fresh.get('expires_in',3600))
            match=re.fullmatch(r'§§secret\(([A-Za-z_][A-Za-z0-9_]*)\)',reference)
            if not match: raise DbHubError('OAuth credentials must be stored in Agent Zero Secrets.')
            extract._write_secret(extract.get_default_secrets_manager(),match[1],_encode(bundle))
        result={**options,'oauth_access_token':tokens['access_token'],'oauth_expires_in':max(1,int((tokens.get('expires_at') or time.time()+3600)-time.time()))}
        result.pop('oauth_credential',None)
        return result


def disconnect(owner,handle):
    with _LOCK:
        _prune()
        if (_FLOWS.get(handle) or {}).get('owner')==owner: del _FLOWS[handle]

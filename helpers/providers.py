"""Provider-guided discovery. Management tokens stay in a short-lived server session.

Only fixed provider GET endpoints are used. Import stores the database password
in A0 Secrets and returns a draft containing a secret reference, never a token.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.helpers import extract, secrets_bridge

BASE_URLS = {'neon': 'https://console.neon.tech/api/v2', 'supabase': 'https://api.supabase.com/v1'}
_SESSIONS = {}
_LOCK = threading.RLock()
TTL = 900


def _id(value):
    value = str(value or '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise DbHubError('Select a valid provider resource from the dropdown.')
    return value


async def _get(provider, token, path, params=None):
    if provider not in BASE_URLS:
        raise DbHubError('Account discovery is available for Neon and Supabase.')
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(BASE_URLS[provider] + path,
                                        headers={'Authorization': 'Bearer ' + token}, params=params)
            if response.status_code != 200:
                raise DbHubError(f'{provider.title()} returned HTTP {response.status_code}. '
                                 'Check the management API token and its project permissions, or use the provider console.')
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Never echo a response body, request URL, header, or token in errors.
        raise DbHubError(f'{provider.title()} discovery could not complete. Try again or use the provider console.') from exc


async def _neon_list(token, path, key, params=None):
    params = {'limit': 100, **(params or {})}
    result = []
    for _ in range(40):
        data = await _get('neon', token, path, params)
        result.extend(data.get(key, []))
        if data.get('unavailable'):
            raise DbHubError('Neon returned an incomplete project list. Retry or choose the project in the Neon console.')
        cursor = (data.get('pagination') or {}).get('next') or (data.get('pagination') or {}).get('cursor')
        if not cursor or not data.get(key):
            return result
        if cursor == params.get('cursor'):
            raise DbHubError('Neon repeated its pagination cursor. Use the provider console.')
        params['cursor'] = cursor
    raise DbHubError('Too many provider resources. Use an organization/project-scoped key or the provider console.')


def _choices(items, key='id'):
    return [{'id': str(item[key]), 'name': str(item.get('name') or item[key])}
            for item in items if item.get(key)]


def _prune():
    now = time.monotonic()
    for key in list(_SESSIONS):
        if _SESSIONS[key]['expires'] <= now:
            del _SESSIONS[key]


def disconnect(owner, handle):
    with _LOCK:
        _prune()
        if (_SESSIONS.get(handle) or {}).get('owner') == owner:
            del _SESSIONS[handle]


def _session(owner, handle, scope):
    with _LOCK:
        _prune()
        value = _SESSIONS.get(handle)
        if not value or value['owner'] != owner or value['scope'] != scope:
            raise DbHubError('Provider session expired or belongs to another settings scope. Connect again.')
        return value


async def connect(owner, provider, token, scope, organization=''):
    if provider not in BASE_URLS:
        raise DbHubError('Use the provider console for this database.')
    token = str(token or '').strip()
    if not token or len(token) > 8192 or '\n' in token or '\r' in token:
        raise DbHubError('Enter a provider management API token or an Agent Zero Secret reference.')
    if '§§secret(' in token:
        token = secrets_bridge.resolve_placeholders(token, project_name=scope[0])
    if provider == 'neon':
        params = {'org_id': _id(organization)} if organization else {}
        projects = await _neon_list(token, '/projects', 'projects', params)
    else:
        projects = await _get(provider, token, '/projects')
    choices = _choices(projects)
    handle = secrets.token_urlsafe(32)
    with _LOCK:
        _prune()
        # One temporary management session per browser; old tokens are discarded.
        for key in list(_SESSIONS):
            if _SESSIONS[key]['owner'] == owner:
                del _SESSIONS[key]
        if len(_SESSIONS) >= 100:
            raise DbHubError('Too many active provider sessions. Try again later.')
        _SESSIONS[handle] = dict(owner=owner, scope=scope, provider=provider, token=token,
                                 projects={p['id']: p for p in projects}, expires=time.monotonic()+TTL,
                                 branches={}, selections={})
    return {'handle': handle, 'projects': choices}


async def resources(owner, handle, scope, project, branch=''):
    state = _session(owner, handle, scope)
    project = _id(project)
    if project not in state['projects']:
        raise DbHubError('Choose a project returned by your connected account.')
    provider, token = state['provider'], state['token']
    if provider == 'supabase':
        pools = await _get(provider, token, f'/projects/{project}/config/database/pooler')
        choices = []
        for index, pool in enumerate(pools):
            mode = str(pool.get('pool_mode') or '').lower()
            if mode not in {'session', 'transaction'}:
                continue
            key = mode + '_' + str(index)
            state['selections'][(project, key)] = pool
            choices.append({'id': key, 'name': mode.title() + ' pooler — ' + str(pool.get('db_host') or pool.get('identifier') or project)})
        if not choices:
            raise DbHubError('Supabase returned no supported pooler. Open this project in its console, click Connect, and copy a Postgres connection string.')
        return {'endpoints': choices}
    if not branch:
        branches = await _neon_list(token, f'/projects/{project}/branches', 'branches')
        state['branches'][project] = {b['id'] for b in branches}
        return {'branches': _choices(branches)}
    branch = _id(branch)
    if branch not in state['branches'].get(project, set()):
        raise DbHubError('Select a branch from the current project.')
    databases, roles = await asyncio.gather(
        _get(provider, token, f'/projects/{project}/branches/{branch}/databases'),
        _get(provider, token, f'/projects/{project}/branches/{branch}/roles'))
    choices = {'databases': _choices(databases.get('databases', []), 'name'),
               'roles': _choices(roles.get('roles', []), 'name')}
    state['selections'][(project, branch)] = choices
    return choices


async def import_connection(owner, handle, scope, data):
    state = _session(owner, handle, scope)
    project = _id(data.get('project'))
    if project not in state['projects']:
        raise DbHubError('Select a project from your connected account.')
    provider = state['provider']
    options = {}
    conn_id = str(data.get('connection_id') or provider)
    if not re.fullmatch(r'[a-z0-9_]+', conn_id):
        raise DbHubError('Connection name must use lowercase letters, numbers, and underscores.')
    if provider == 'neon':
        branch = _id(data.get('branch'))
        selected = state['selections'].get((project, branch), {})
        database, role = data.get('database'), data.get('role')
        if database not in [d['id'] for d in selected.get('databases', [])] or role not in [r['id'] for r in selected.get('roles', [])]:
            raise DbHubError('Choose the database and role from the current branch.')
        result = await _get(provider, state['token'], f'/projects/{project}/connection_uri',
                            {'branch_id': branch, 'database_name': database, 'role_name': role, 'pooled': 'false'})
        dsn = str(result.get('uri') or '')
    else:
        pool = state['selections'].get((project, str(data.get('endpoint') or '')))
        if not pool:
            raise DbHubError('Select a returned pooler for this project.')
        if pool.get('pool_mode') == 'transaction':
            options['statement_cache_size'] = 0
        password = str(data.get('database_password') or '')
        if not password:
            raise DbHubError('Supabase requires the existing database password. Its management API cannot retrieve it.')
        # Use the provider-returned connection template; never guess pooler hosts or users.
        template = str(pool.get('connection_string') or pool.get('connectionString') or '')
        if template:
            # Supabase templates contain literal brackets in the password slot,
            # which urlsplit otherwise mistakes for an IPv6 address.
            template = template.replace('[YOUR-PASSWORD]', 'placeholder').replace('[YOUR_PASSWORD]', 'placeholder')
            parsed = urlsplit(template)
            host, user, port, path = parsed.hostname, unquote(parsed.username or ''), parsed.port, parsed.path
        else:
            host, user, port, path = pool.get('db_host'), pool.get('db_user'), pool.get('db_port'), '/' + str(pool.get('db_name') or 'postgres')
        if not host or not user or not port:
            raise DbHubError('Supabase did not return full connection details. Use the Connect dialog in its console.')
        if ':' in host:
            host = '[' + host + ']'
        dsn = urlunsplit(('postgresql', f'{quote(str(user), safe="")}:{quote(password, safe="")}@{host}:{int(port)}', path, 'sslmode=require', ''))
    parsed = urlsplit(dsn)
    if parsed.scheme not in {'postgres', 'postgresql'} or not parsed.hostname or not parsed.password:
        raise DbHubError('The provider did not return a complete Postgres connection. Use its console.')
    # Add a random suffix so another connection's credential can never be overwritten.
    secret_key = 'DB_HUB_' + provider.upper() + '_' + secrets.token_hex(8).upper()
    rewritten = extract.extract_dsn_secret(dsn, key=secret_key, conn_id=conn_id)
    return {'connection': {'id': conn_id, 'driver': 'postgres', 'dsn': rewritten['dsn'],
                           'enabled': True, 'readonly': None, 'options': options,
                           'description': provider.title() + ': ' + str(state['projects'][project].get('name') or project)},
            'notice': 'Database password stored in Agent Zero Secrets. Test and Save the draft to activate it.'}

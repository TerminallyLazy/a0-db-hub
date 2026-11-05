"""First-time identity creation; tokens go directly to scoped Agent Zero Secrets.

No token is returned to the browser. A non-secret receipt recovers an unsaved
draft and a file lock serializes overlapping tabs/processes for that draft.
"""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import secrets

import httpx
from helpers.secrets import get_default_secrets_manager, get_project_secrets_manager
from usr.plugins.db_hub.helpers import config, extract, registry
from usr.plugins.db_hub.helpers.base import DbHubError
from usr.plugins.db_hub.helpers.drivers.spacetimedb import server_origin


def _receipt_path(origin, connection, project, profile):
    from helpers import files, plugins
    key = hashlib.sha256(json.dumps([origin, connection, project, profile]).encode()).hexdigest()
    return Path(files.get_abs_path(plugins.find_plugin_dir('db_hub'), '.identity-setups', key + '.json'))


def _record(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as file:
        temp.chmod(0o600)
        json.dump(value, file)
    temp.replace(path)


async def _verify(client, identity, token):
    response = await client.get('/v1/identity/' + identity + '/verify',
                                headers={'Authorization': 'Bearer ' + token})
    if response.status_code != 204:
        raise DbHubError('SpacetimeDB could not verify the stored identity. Restore its original token in Secrets; no replacement identity was created.')


def _result(origin, receipt, recovered=False):
    return {'ok': True, 'server': origin, 'identity': receipt['identity'], 'key': receipt['key'],
            'auth_token': '§§secret(' + receipt['key'] + ')', 'recovered': recovered,
            'notice': ('Existing identity recovered' if recovered else 'Identity created') +
                ' and its token stored in Agent Zero Secrets. Choose Read & write, review memory storage, then Test and Save. Keep this token to retain access to these memories.'}


async def create_identity(entry, project='', profile=''):
    entry = config.normalize_for_save({'connections': [entry]})['connections'][0]
    if entry['driver'] != 'spacetimedb':
        raise DbHubError('Identity creation is available only for SpacetimeDB.')
    origin = server_origin(entry['dsn'])
    if entry['options'].get('auth_token') or entry['options'].get('memory_database'):
        raise DbHubError('Reuse the existing token for this connection or memory database. Create a separate connection for a new identity.')
    saved = registry.load_config(None, project_name=project, agent_profile=profile)
    previous = next((c for c in saved.get('connections', []) if c['id'] == entry['id']), None)
    if previous and (previous.get('options', {}).get('auth_token') or previous.get('options', {}).get('memory_database')):
        raise DbHubError('This saved connection already has an identity or memory database. Reopen settings to reuse it, or create a separate connection.')
    manager = get_project_secrets_manager(project) if project else get_default_secrets_manager()
    path = _receipt_path(origin, entry['id'], project, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DbHubError('Identity creation is already in progress for this connection. Try again when it finishes.') from None
        try:
            async with httpx.AsyncClient(base_url=origin, timeout=30, follow_redirects=False) as client:
                if path.exists():
                    receipt = json.loads(path.read_text())
                    token = manager.load_secrets().get(receipt['key'])
                    if token:
                        await _verify(client, receipt['identity'], token)
                        return _result(origin, receipt, True)
                    if receipt.get('state') == 'stored':
                        raise DbHubError('The previously created identity token is missing from Secrets. Restore the original token; a new identity would use separate memories.')
                response = await client.post('/v1/identity')
                if not response.is_success:
                    raise DbHubError(f'SpacetimeDB identity creation returned HTTP {response.status_code}. Check this server and its identity-creation policy, or use an existing token.')
                data = response.json()
                identity = data.get('identity', '') if isinstance(data, dict) else ''
                identity = identity.removeprefix('0x') if isinstance(identity, str) else ''
                token = data.get('token') if isinstance(data, dict) else None
                if not re.fullmatch(r'[0-9a-fA-F]{64}', identity) or not isinstance(token, str) or not token or len(token) > 2048 or any(c.isspace() or c == "'" for c in token):
                    raise DbHubError('SpacetimeDB returned an unsupported identity response. No token was attached to this connection.')
                await _verify(client, identity, token)
                name = 'DB_HUB_' + entry['id'].upper()[:24] + '_ST_' + secrets.token_hex(8).upper()
                receipt = {'identity': identity, 'key': name, 'state': 'received'}
                _record(path, receipt)
                # The manager lock covers collision checking and the merge-preserving
                # write. Project requests write only that project's secrets file.
                with manager._lock:
                    extract.store_secret(name, token, manager=manager)
                receipt['state'] = 'stored'
                _record(path, receipt)
                return _result(origin, receipt)
        except (httpx.HTTPError, ValueError):
            # Provider bodies/headers and tokens must never reach API errors/logs.
            raise DbHubError('SpacetimeDB identity setup could not finish. Check the server/network, then retry to recover any stored token. No database was created.') from None

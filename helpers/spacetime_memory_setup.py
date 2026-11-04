"""Reviewed creation of a new private SpacetimeDB memory database.

POST /v1/database always creates a fresh identity. Never PUT an application
module or republish an existing database, including in retries.
"""
import hashlib
import json
from pathlib import Path
import re
import time

from usr.plugins.db_hub.helpers import config, registry
from usr.plugins.db_hub.helpers.base import DbHubError, ReadOnlyError
from usr.plugins.db_hub.helpers.explorer.service import review, consume


def _receipt_path(entry, project, profile):
    from helpers import files, plugins
    key = hashlib.sha256(json.dumps([project, profile, entry['id'], registry._fingerprint(entry)]).encode()).hexdigest()
    return Path(files.get_abs_path(plugins.find_plugin_dir('db_hub'), '.memory-setups', key + '.json'))


def _save_receipt(entry, project, profile, identity, digest):
    path = _receipt_path(entry, project, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'memory_database': identity, 'module': digest, 'created_at': time.time()}))
    temp.chmod(0o600)
    temp.replace(path)


def _previous(path):
    if not path.exists():
        return None
    previous = json.loads(path.read_text())
    if not previous.get('memory_database'):
        raise DbHubError('A memory database creation was started, but its outcome is unknown. Inspect this server for the created database before retrying; no additional database was created by this request.')
    return previous['memory_database']


def module_bundle():
    path = Path(__file__).resolve().parents[1] / 'memory_modules/spacetimedb'
    receipt = json.loads((path / 'module.manifest.json').read_text())
    data = (path / 'module.js').read_bytes()
    if hashlib.sha256(data).hexdigest() != receipt['sha256']:
        raise DbHubError('SpacetimeDB memory module checksum failed. Reinstall DB Hub.')
    for name, expected in receipt['inputs'].items():
        if hashlib.sha256((path / name).read_bytes()).hexdigest() != expected:
            raise DbHubError('SpacetimeDB memory module is stale. Rebuild it before provisioning a database.')
    return data, receipt['sha256']


async def _connection(entry, project, profile):
    normalized = config.normalize_for_save({'connections': [entry]})
    entry = normalized['connections'][0]
    if entry['driver'] != 'spacetimedb':
        raise DbHubError('Memory module setup is available only for SpacetimeDB.')
    if registry.effective_readonly(entry, normalized, None):
        raise ReadOnlyError('Choose Read & write for this draft before creating memory storage.')
    if not entry['options'].get('auth_token'):
        raise DbHubError('Store a stable SpacetimeDB Bearer token in Agent Zero Secrets first.')
    if entry['options'].get('memory_database'):
        raise DbHubError('A memory database is already configured. Test and Save it; clear that field only if you intend to create a separate new database.')
    return await registry.get_connection(entry['id'], project_name=project, agent_profile=profile,
                                         entry_override=entry, pool=False)


async def review_setup(owner, entry, project='', profile=''):
    bound = await _connection(entry, project, profile)
    try:
        _, digest = module_bundle()
        await bound.test()
        path = _receipt_path(bound.conn_cfg, project, profile)
        previous = _previous(path)
        if previous:
            return {'connection': bound.conn_cfg['id'], 'recovery': previous,
                    'notice': 'A memory database was already created for this connection. Attach its identity to this draft, then Test and Save. No additional database will be created.'}
        credential = hashlib.sha256(bound.driver._options['auth_token'].encode()).hexdigest()
        payload = {'entry': bound.conn_cfg, 'module': digest, 'credential': credential}
        token = review((owner, project, profile, bound.conn_cfg['id']), payload)
        return {'token': token, 'connection': bound.conn_cfg['id'],
                'server': str(bound.driver._client.base_url).rstrip('/'), 'expires_in': 300,
                'notice': 'Create one new private memory database on this server using this identity. This can consume provider storage/quota. The application module will not be replaced. Creation is separate from Save.'}
    finally:
        await bound.driver.close()


async def create_storage(owner, token, connection, project='', profile=''):
    payload = consume((owner, project, profile, connection), token)
    bound = await _connection(payload['entry'], project, profile)
    try:
        bundle, digest = module_bundle()
        credential = hashlib.sha256(bound.driver._options['auth_token'].encode()).hexdigest()
        if digest != payload['module'] or credential != payload['credential']:
            raise DbHubError('The module or credential changed. Review memory setup again.')
        path = _receipt_path(bound.conn_cfg, project, profile)
        previous = _previous(path)
        if previous:
            return {'ok': True, 'ready': False, 'memory_database': previous,
                    'notice': 'Existing setup recovered. Test and Save this memory database; no additional database was created.'}
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive creation also protects two simultaneous reviewed browser tabs.
            with path.open('x') as receipt:
                path.chmod(0o600)
                json.dump({'state': 'started', 'module': digest, 'created_at': time.time()}, receipt)
        except FileExistsError:
            raise DbHubError('Memory setup is already in progress. Review again after it finishes.') from None
        response = await bound.driver._request('POST', database_path='/v1/database',
                        params={'host_type': 'Js'}, content=bundle,
                        headers={'Content-Type': 'application/javascript'})
        raw = bound.driver._json(response)
        identity = (raw.get('Success') or {}).get('database_identity') if isinstance(raw, dict) else None
        if isinstance(identity, dict):
            identity = identity.get('__identity__')
        identity = str(identity or '').removeprefix('0x')
        if not re.fullmatch(r'[0-9a-fA-F]{64}', identity):
            raise DbHubError('SpacetimeDB may have created storage, but returned an unexpected identity. Inspect the provider console before creating another database.')
        _save_receipt(bound.conn_cfg, project, profile, identity, digest)
        bound.driver._memory_path = '/v1/database/' + identity
        try:
            await bound.driver.memory_info()
        except Exception as exc:
            return {'ok': True, 'ready': False, 'memory_database': identity,
                    'notice': bound.mask('Storage was created, but verification failed: ' + str(exc) + '. Its identity is retained in the draft; Test before enabling memory. Do not create another database to retry verification.')}
        return {'ok': True, 'ready': True, 'memory_database': identity,
                'notice': 'Private memory database created and verified. Select this connection for Agent memory and click Save to prepare all memory areas and persist the settings.'}
    finally:
        await bound.driver.close()

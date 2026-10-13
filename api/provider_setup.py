"""Authenticated, CSRF-protected provider discovery and draft import."""
import secrets
from flask import session
from helpers.api import ApiHandler
from usr.plugins.db_hub.helpers import providers
from usr.plugins.db_hub.helpers.base import DbHubError


class ProviderSetup(ApiHandler):
    async def process(self, input, request):
        owner = session.get('db_hub_provider_owner')
        if not owner:
            owner = session['db_hub_provider_owner'] = secrets.token_urlsafe(32)
        scope = (str(input.get('project_name') or ''), str(input.get('agent_profile') or ''))
        action = input.get('action')
        try:
            if action == 'connect':
                result = await providers.connect(owner, input.get('provider'), input.get('token'), scope, input.get('organization', ''))
            elif action == 'resources':
                result = await providers.resources(owner, input.get('handle'), scope, input.get('project'), input.get('branch', ''))
            elif action == 'import':
                result = await providers.import_connection(owner, input.get('handle'), scope, input)
            elif action == 'disconnect':
                providers.disconnect(owner, input.get('handle'))
                result = {}
            else:
                raise DbHubError('Unknown provider setup action.')
            return {'ok': True, **result}
        except DbHubError as exc:
            return {'ok': False, 'error': str(exc)}
        except Exception:
            return {'ok': False, 'error': 'Provider setup could not complete. Retry or use the provider console.'}

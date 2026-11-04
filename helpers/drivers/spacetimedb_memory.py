"""Private, caller-owned memory protocol implemented by the shipped module."""
import json
import math
import uuid

from usr.plugins.db_hub.helpers.base import DbHubError

ERRORS = {
    'collection_missing': 'Memory collection does not exist. Save the memory settings to prepare it.',
    'dimension_mismatch': 'Memory embedding dimension differs from the saved collection. Choose a new memory prefix.',
    'invalid_vector': 'Memory embeddings must contain finite numbers.',
    'invalid_dimension': 'Memory embedding dimension must be between 1 and 65536.',
    'invalid_collection': 'Memory collection names must use letters, numbers and underscores (up to 256 characters).',
    'unsupported_filter': 'SpacetimeDB memory supports flat equality filters with scalar values.',
    'scan_limit': 'SpacetimeDB memory scan limit exceeded; increase options.memory_max_scan (up to 50000) or choose a native vector store. No partial result was returned.',
    'delete_selector_required': 'Memory deletion requires IDs or a non-empty filter.',
    'invalid_limit': 'Invalid memory result, batch or scan limit.',
    'invalid_batch': 'Memory upsert requires between 1 and 100 items.',
    'invalid_id': 'Memory IDs must be non-empty strings, up to 256 characters.',
    'invalid_payload': 'Memory payload must be a JSON object.',
    'payload_too_large': 'Memory payload exceeds 256 KB.',
}


class SpacetimeMemory:
    async def _memory_rpc(self, operation, args):
        if not self._options.get('auth_token'):
            raise DbHubError('SpacetimeDB memory requires a stable Bearer token stored in Agent Zero Secrets.')
        if not self._memory_path:
            raise DbHubError('Prepare a dedicated SpacetimeDB memory database in connection settings first.')
        response = await self._request('POST', '/call/dbhub_memory_' + operation,
                    database_path=self._memory_path, params={'confirmed': 'true'}, json=args)
        raw = self._json(response)
        try:
            data = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get('ok'), bool):
            raise DbHubError('SpacetimeDB memory database does not implement the DB Hub memory protocol. Check its module.')
        if not data['ok']:
            raise DbHubError(ERRORS.get(data.get('error'), 'SpacetimeDB memory operation failed. Check the memory module and server logs.'))
        return data

    async def memory_info(self):
        data = await self._memory_rpc('info', [])
        if data.get('protocol') != 'a0_db_hub_memory' or data.get('version') != 1 or not isinstance(data.get('collections'), list):
            raise DbHubError('Unsupported SpacetimeDB memory module version. Install the module shipped with DB Hub.')
        return data

    def _memory_scan_limit(self):
        try:
            value = int(self._options.get('memory_max_scan', 10000))
        except (ValueError, TypeError):
            raise DbHubError('options.memory_max_scan must be an integer between 1 and 50000.') from None
        if not 1 <= value <= 50000:
            raise DbHubError('options.memory_max_scan must be between 1 and 50000.')
        return value

    @staticmethod
    def _memory_vector(vector):
        if not isinstance(vector, list) or not 1 <= len(vector) <= 65536:
            raise DbHubError('Memory vector must contain between 1 and 65536 numbers.')
        try:
            values = [float(v) for v in vector]
        except (TypeError, ValueError):
            raise DbHubError('Memory vector must contain finite numbers.') from None
        if not all(math.isfinite(v) for v in values):
            raise DbHubError('Memory vector must contain finite numbers.')
        return values

    @staticmethod
    def _memory_filter(flt):
        if flt is not None and not isinstance(flt, dict):
            raise DbHubError(ERRORS['unsupported_filter'])
        try:
            return json.dumps(flt or {}, allow_nan=False)
        except (ValueError, TypeError):
            raise DbHubError(ERRORS['unsupported_filter']) from None

    async def ensure_collection(self, collection, dim):
        await self.memory_info()
        if isinstance(dim, bool) or not isinstance(dim, int) or not 1 <= dim <= 65536:
            raise DbHubError(ERRORS['invalid_dimension'])
        await self._memory_rpc('ensure', [collection, dim])

    async def vector_search(self, collection, vector, top_k, flt=None):
        await self.memory_info()
        data = await self._memory_rpc('search', [collection, self._memory_vector(vector),
                    max(1, min(int(top_k or 10), 100)), self._memory_filter(flt), self._memory_scan_limit()])
        return {'matches': [{**item['payload'], 'id': item['id'], 'score': item['score'], '_score': item['score']}
                            for item in data['matches']]}

    async def vector_upsert(self, collection, items):
        await self.memory_info()
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise DbHubError(ERRORS['invalid_batch'])
        encoded = []
        for item in items:
            payload = dict(item.get('payload') or {})
            if 'text' in item and 'text' not in payload:
                payload['text'] = item['text']
            try:
                text = json.dumps(payload, allow_nan=False)
            except (TypeError, ValueError):
                raise DbHubError(ERRORS['invalid_payload']) from None
            encoded.append({'id': str(item.get('id') or uuid.uuid4()),
                            'vector': self._memory_vector(item.get('vector')), 'payload': text})
        data = await self._memory_rpc('upsert', [collection, encoded])
        return {'upserted': data['upserted']}

    async def vector_delete(self, collection, ids=None, flt=None):
        await self.memory_info()
        if not ids and not flt:
            raise DbHubError(ERRORS['delete_selector_required'])
        if ids and flt:
            raise DbHubError('Choose IDs or a memory filter, not both.')
        if ids is not None and (not isinstance(ids, list) or len(ids) > 1000):
            raise DbHubError('Memory deletion accepts up to 1000 IDs.')
        data = await self._memory_rpc('delete', [collection, [str(i) for i in ids or []],
                    self._memory_filter(flt), self._memory_scan_limit()])
        return {'deleted': data['deleted']}

    async def memory_page(self, collection, limit=50, offset=0):
        await self.memory_info()
        data = await self._memory_rpc('list', [collection, max(1, min(int(limit), 100)),
                    max(0, int(offset)), self._memory_scan_limit()])
        return {'rows': [{**row['payload'], 'id': row['id'], 'vector': row['vector']} for row in data['rows']],
                'next_offset': data['next_offset']}

"""Bounded, typed explorer contracts shared by provider adapters."""
import base64
import datetime
import decimal
import hashlib
import json
import math
import uuid
from usr.plugins.db_hub.helpers.base import DbHubError


def plain(value):
    if isinstance(value, dict): return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [plain(v) for v in value]
    if isinstance(value, int) and abs(value) > 9007199254740991: return str(value)
    if isinstance(value, float) and not math.isfinite(value): return str(value)
    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)): return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID)): return str(value)
    if isinstance(value, bytes): return {'$binary': base64.b64encode(value).decode()}
    if hasattr(value, 'model_dump'): return plain(value.model_dump(mode='json'))
    if hasattr(value, 'to_dict'): return plain(value.to_dict())
    if hasattr(value, 'tolist'): return plain(value.tolist())
    return value


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def limit(data): return max(1, min(int(data.get('limit', 50)), 100))
def offset(data): return max(0, min(int(data.get('offset', 0)), 100000))
def quote(name): return '"' + str(name).replace('"', '""') + '"'
def obj(value):
    if not isinstance(value, dict): raise DbHubError('Enter a JSON object.')
    if len(json.dumps(value, default=str)) > 256000: raise DbHubError('Record exceeds the 256 KB editor limit.')
    return value

def conflict(): raise DbHubError('This record changed or disappeared. Refresh it before reviewing changes again.')

def caps(*, page='offset', filter=True, sort=True, insert=False, update=False, delete=False, visual=False, note=''):
    return dict(page=page, filter=filter, sort=sort, insert=insert, update=update, delete=delete, visual=visual, note=note)


def checked_columns(meta, values, operation):
    values = obj(values)
    cols = {c['name']: c for c in meta['columns']}
    if any(k not in cols for k in values): raise DbHubError('The record contains unknown columns. Refresh the structure.')
    for key in values:
        if cols[key].get('generated') or (operation != 'insert' and key in meta.get('primary_key', [])):
            raise DbHubError(f'{key} is generated or part of the record identity and cannot be edited here.')
    return values


def filters(data, meta):
    result = data.get('filters') or []
    if not isinstance(result, list) or len(result) > 12: raise DbHubError('Use at most 12 filters.')
    names = {c['name'] for c in meta['columns']}
    for f in result:
        if not isinstance(f, dict) or f.get('column') not in names or f.get('op') not in {'eq','ne','lt','lte','gt','gte','contains','null','notnull'}:
            raise DbHubError('Choose a returned column and a supported filter operator.')
    return result

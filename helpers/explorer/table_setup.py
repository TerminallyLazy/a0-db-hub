"""Bounded table design, compiled DDL and creation in an existing database."""
import asyncio
import re
from .common import DbHubError, quote

KINDS = {'postgres', 'sqlalchemy', 'duckdb', 'snowflake', 'bigquery'}
LABELS = {'auto_id': 'Automatic ID', 'integer': 'Whole number', 'text': 'Text', 'boolean': 'True / false', 'decimal': 'Decimal (18, 6)', 'float': 'Floating point', 'date': 'Date', 'timestamp': 'Date & time', 'json': 'JSON', 'uuid': 'UUID'}


async def options(bound):
    kind = bound.conn_cfg['driver']; d = bound.driver
    if kind == 'spacetimedb':
        return {'supported': False, 'note': 'SpacetimeDB tables are defined in a Rust, TypeScript, C# or C++ module and published with the spacetime CLI. Use Agent help to design the module; CREATE TABLE is unavailable.'}
    if kind not in KINDS:
        return {'supported': False, 'note': 'This engine uses collections, keys, graphs or functions. Agent help provides a native setup prompt.'}
    if kind == 'postgres':
        async with d._pool.acquire() as c:
            rows = await c.fetch("SELECT nspname AS name FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' AND has_schema_privilege(oid,'USAGE') AND has_schema_privilege(oid,'CREATE') ORDER BY nspname")
        schemas = [r['name'] for r in rows]
    elif kind == 'sqlalchemy':
        def read():
            import sqlalchemy as sa
            with d._engine.connect() as c:
                return [s for s in sa.inspect(c).get_schema_names() if s not in ('information_schema', 'pg_catalog', 'sys')]
        schemas = await asyncio.to_thread(read)
    elif kind == 'bigquery':
        schemas = [d._dataset] if d._dataset else await asyncio.to_thread(lambda: [v.dataset_id for v in d._client.list_datasets()])
    else:
        result = await bound.query('SELECT schema_name AS name FROM information_schema.schemata ORDER BY schema_name', readonly=True)
        schemas = [r.get('name', r.get('NAME')) for r in result['rows'] if str(r.get('name', r.get('NAME'))).lower() not in ('information_schema', 'pg_catalog')]
    types = list(LABELS)
    if kind in ('duckdb', 'bigquery'): types.remove('auto_id')
    if kind not in ('postgres', 'duckdb'): types.remove('uuid')
    if kind == 'sqlalchemy' and d._engine.dialect.name not in ('sqlite', 'postgresql', 'mysql', 'mariadb', 'mssql'):
        return {'supported': False, 'note': 'Use Agent help and the Query editor for this SQL dialect.'}
    return {'supported': True, 'schemas': schemas, 'types': [{'id': t, 'label': LABELS[t]} for t in types], 'keys_enforced': kind not in ('snowflake', 'bigquery'), 'note': 'Primary keys are declared but not enforced by this warehouse.' if kind in ('snowflake', 'bigquery') else ''}


def normalize(spec, choices):
    if not isinstance(spec, dict): raise DbHubError('Enter a table definition.')
    def name(value):
        value = str(value or '').strip()
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', value): raise DbHubError('Names must start with a lowercase letter or underscore and contain only lowercase letters, numbers and underscores (up to 63 characters).')
        return value
    schema = spec.get('schema')
    if schema not in choices.get('schemas', []): raise DbHubError('Choose an available schema or dataset. You may need CREATE permission.')
    raw = spec.get('columns')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50: raise DbHubError('Add between 1 and 50 columns.')
    columns = []; seen = set(); types = {x['id'] for x in choices['types']}
    for col in raw:
        if not isinstance(col, dict): raise DbHubError('Invalid column definition.')
        key = name(col.get('name')); typ = col.get('type')
        if key in seen: raise DbHubError(f"Column '{key}' appears twice.")
        if typ not in types: raise DbHubError('Choose a supported column type.')
        seen.add(key); primary = col.get('primary') is True or typ == 'auto_id'
        columns.append({'name': key, 'type': typ, 'primary': primary, 'nullable': col.get('nullable') is True and not primary})
    if not any(c['primary'] for c in columns): raise DbHubError('Choose a primary key so records have a stable identity for editing.')
    if any(c['type'] == 'auto_id' for c in columns) and sum(c['primary'] for c in columns) != 1: raise DbHubError('An automatic ID must be the only primary key column.')
    return {'schema': schema, 'name': name(spec.get('name')), 'columns': columns}


def compile_sql(bound, spec):
    kind = bound.conn_cfg['driver']
    if kind == 'sqlalchemy':
        import sqlalchemy as sa
        types = {'auto_id': sa.Integer, 'integer': sa.BigInteger, 'text': sa.Text, 'boolean': sa.Boolean, 'decimal': lambda: sa.Numeric(18, 6), 'float': sa.Float, 'date': sa.Date, 'timestamp': sa.DateTime, 'json': sa.JSON}
        cols = [sa.Column(c['name'], types[c['type']](), primary_key=c['primary'], nullable=c['nullable'], autoincrement=c['type'] == 'auto_id') for c in spec['columns']]
        table = sa.Table(spec['name'], sa.MetaData(), *cols, schema=spec['schema'])
        return str(sa.schema.CreateTable(table).compile(dialect=bound.driver._engine.dialect)).strip()
    types = {'auto_id': 'BIGINT GENERATED BY DEFAULT AS IDENTITY', 'integer': 'BIGINT', 'text': 'TEXT', 'boolean': 'BOOLEAN', 'decimal': 'DECIMAL(18,6)', 'float': 'DOUBLE PRECISION', 'date': 'DATE', 'timestamp': 'TIMESTAMP', 'json': 'JSONB', 'uuid': 'UUID'}
    if kind == 'duckdb': types.update(json='JSON')
    if kind == 'snowflake': types.update(auto_id='NUMBER AUTOINCREMENT', json='VARIANT', timestamp='TIMESTAMP_NTZ')
    if kind == 'bigquery': types.update(integer='INT64', text='STRING', boolean='BOOL', decimal='NUMERIC', float='FLOAT64', json='JSON')
    def q(value):
        if kind == 'bigquery':
            if '`' in value: raise DbHubError('Unsupported identifier.')
            return '`' + value + '`'
        return quote(value)
    table = q(spec['schema'] + '.' + spec['name']) if kind == 'bigquery' else q(spec['schema']) + '.' + q(spec['name'])
    columns = [q(c['name']) + ' ' + types[c['type']] + ('' if c['nullable'] else ' NOT NULL') for c in spec['columns']]
    columns.append('PRIMARY KEY (' + ', '.join(q(c['name']) for c in spec['columns'] if c['primary']) + ')' + (' NOT ENFORCED' if kind == 'bigquery' else ''))
    return 'CREATE TABLE ' + table + ' (\n  ' + ',\n  '.join(columns) + '\n)'

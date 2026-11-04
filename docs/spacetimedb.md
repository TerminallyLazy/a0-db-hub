# SpacetimeDB

DB Hub connects to an existing published SpacetimeDB database through its [HTTP API](https://spacetimedb.com/docs/http/database/). It uses Agent Zero's existing HTTP client; no SpacetimeDB CLI, generated bindings or additional Python package is needed inside Agent Zero.

## Connect

1. Publish your application module using the [SpacetimeDB CLI](https://spacetimedb.com/docs/cli-reference/). DB Hub does not create a server, database or module.
2. In DB Hub's catalog, search **SpacetimeDB** and add a connection draft.
3. Set the connection URL to the server origin, such as `https://maincloud.spacetimedb.com` or `http://localhost:3000`. Do not append `/v1/database/...` or credentials. When Agent Zero runs in Docker and the server runs on its host, use `http://host.docker.internal:3000` where supported; Linux hosts may require a host-gateway mapping or an accessible server address.
4. Enter the published database **name or identity**. Advanced configuration stores this as `options.database`.
5. For authenticated operations, enter an identity Bearer token and move it into Agent Zero Secrets using **Move to Secrets**. Only the `§§secret(KEY)` reference is saved as `options.auth_token`. Use the CLI's token display locally when needed; do not paste credentials into agent chat. Token refresh is manual.
6. **Test**, then **Save**. Test checks database metadata and its schema. Individual table/reducer access is governed by the identity and module. Leave the connection read-only for exploration; explicitly allow writes when reducer calls are intended.

Example configuration (edit the server and database):

```yaml
connections:
  - id: spacetime_app
    driver: spacetimedb
    dsn: https://maincloud.spacetimedb.com
    readonly: true
    options:
      database: my-database
      auth_token: §§secret(SPACETIME_TOKEN)
```

The token is optional for public reads. Without one, the server allocates a new anonymous identity for each HTTP request. Operations that use `ctx.sender` for ownership need a stable authenticated identity. A schema can list private tables even when that identity cannot read them. See [authorization](https://spacetimedb.com/docs/http/authorization/).

## Browse and query

The explorer shows public/private table declarations and callable reducers. Table Structure includes column types, primary keys, indexes and constraints. Data shows a bounded first-page sample. Use **Query** for SQL `WHERE` and `LIMIT`; offset pagination and sorting are unavailable. Relationships are not inferred.

Agent tools use the same saved connection:

```json
{"tool_name":"db_schema","tool_args":{"connection":"spacetime_app"}}
```

```json
{"tool_name":"db_query","tool_args":{"connection":"spacetime_app","statement":"SELECT * FROM record LIMIT 20"}}
```

SpacetimeDB has a [SQL subset](https://spacetimedb.com/docs/reference/sql/). There are no bound SQL parameters, `ORDER BY`, `OFFSET` or `CREATE TABLE` in this adapter. DB Hub permits one statement at a time and conservatively rejects embedded semicolons. Prefer reducer arguments for arbitrary text. Tables, indexes, schema migrations and application authorization belong in the module and its publish workflow.

## Call reducers

For application writes, select a reducer in the explorer or use `db_query` with **DB Hub syntax** `CALL reducer_name`. The adapter translates this to SpacetimeDB's HTTP call endpoint. Arguments are a positional JSON array in schema order:

```json
{"tool_name":"db_query","tool_args":{"connection":"spacetime_app","statement":"CALL add_record","params":{"args":["hello from Agent Zero"]}}}
```

The saved connection must allow writes. In the workspace, **Allow changes** and confirmation of the exact statement/arguments are also required. A tool's `readonly:false` cannot override saved read-only access. Every reducer call is treated as a write, even when its name suggests a read.

Reducers return no rows. Read the affected table afterward to inspect the result. `params.confirmed` is a boolean that defaults to `true` for durability. A timeout or lost connection can leave a write's outcome unknown; read back before retrying. The adapter never automatically retries a reducer call.

Inspect argument types with `db_schema`. Values use [SATS-JSON](https://spacetimedb.com/docs/sats-json/) shapes. DB Hub accepts decimal strings for 64-bit and 128-bit integer argument fields, including those nested in structs, arrays or sums, and encodes them as exact numeric JSON using the current reducer schema. Numeric text fields remain strings. SQL integers outside JavaScript's safe range are returned as decimal strings to preserve their value in the workspace.

SpacetimeDB is available for ordinary database work. It is not an automatic-memory destination. Live WebSocket subscriptions, procedures, module publishing and account discovery are outside this adapter's scope. An independently configured SpacetimeDB MCP server can coexist with DB Hub.

## Reproduce the local acceptance check

Use a disposable server and fixture database. Run this in an Agent Zero v2.14 checkout with DB Hub under `usr/plugins/db_hub`. The fixture SDK/compiler dependencies are needed only to build the test module:

```sh
cp -R usr/plugins/db_hub/tests/fixtures/spacetimedb /tmp/dbhub-spacetime-fixture
cd /tmp/dbhub-spacetime-fixture
npm install --no-audit --no-fund
spacetime start --listen-addr 0.0.0.0:43127 --data-dir /tmp/dbhub-spacetime-data --in-memory --non-interactive
```

In another terminal:

```sh
spacetime publish dbhub-spacetime-check --server http://127.0.0.1:43127 --module-path /tmp/dbhub-spacetime-fixture --anonymous --yes --no-config
cd /a0
DB_HUB_SPACETIME_URL=http://127.0.0.1:43127 \
  /opt/venv-a0/bin/python -m pytest -q usr/plugins/db_hub/tests/test_spacetimedb.py usr/plugins/db_hub/tests/test_spacetimedb_live.py
node --test usr/plugins/db_hub/tests/*.test.mjs
```

If the framework is in Docker and SpacetimeDB is on its host, set the test URL to `http://host.docker.internal:43127` instead. The live test creates fresh server identities and a temporary Agent Zero project, saves Secret-backed scoped connections through the real configuration hook, resolves tools with `Agent.get_tool`, runs their execution/history hooks, and verifies SQL/reducer readback. It checks sender authorization, private access, read-only enforcement, rollback, wide integers and the workspace's confirmation gate. No model inference or Maincloud account is used. Stop the fixture server afterward; its data is in memory.

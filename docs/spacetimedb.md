# SpacetimeDB

DB Hub connects to an existing published SpacetimeDB database through its [HTTP API](https://spacetimedb.com/docs/http/database/). It uses Agent Zero's existing HTTP client; no SpacetimeDB CLI, generated bindings or additional Python package is needed inside Agent Zero.

## Connect

1. Publish your application module using the [SpacetimeDB CLI](https://spacetimedb.com/docs/cli-reference/). DB Hub connects to that existing application; the optional memory setup below creates a separate database.
2. In DB Hub's catalog, search **SpacetimeDB** and add a connection draft.
3. Set the connection URL to the server origin, such as `https://maincloud.spacetimedb.com` or `http://localhost:3000`. Do not append `/v1/database/...` or credentials. When Agent Zero runs in Docker and the server runs on its host, use `http://host.docker.internal:3000` where supported; Linux hosts may require a host-gateway mapping or an accessible server address.
4. Enter the published database **name or identity**. Advanced configuration stores this as `options.database`.
5. For first-time memory setup, open **Agent memory storage → Create identity and store token**. DB Hub creates a host identity and saves its token directly in Agent Zero Secrets for the selected project (or global scope). Only the `§§secret(KEY)` reference returns to settings. Alternatively, enter an existing identity Bearer token and use **Move to Secrets**; retain the original token for existing memory data. Token refresh is manual.
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

Live WebSocket subscriptions, arbitrary procedure calls, application-module publishing and account discovery are outside the query adapter's scope. An independently configured SpacetimeDB MCP server can coexist with DB Hub.

## Agent Zero memory (1.2.0)

SpacetimeDB supports all four memory tools, automatic recall, background fragment/solution learning, replication to other supported memory stores, and explicit FAISS migration. Keep Agent Zero's built-in Memory plugin enabled for prompts and recall/learning settings. Its dashboard still describes built-in storage; browse external data in DB Hub.

1. Choose **Create identity and store token** in **Agent memory storage** for a new memory identity, or enter an existing token and **Move to Secrets**. Choose **Read & write**. Anonymous HTTP requests cannot retain caller-owned memory across requests.
2. Turn on **Use database for memory** to open storage setup for a single unprepared SpacetimeDB connection, or click **Set up memory** beside its name. You can also expand **Agent memory storage** on the connection. **Review new storage**, review the server and storage/quota notice, then **Create private database**. DB Hub uploads the bundled private module to a **new database identity**, using POST creation only. It never republishes or overwrites the application database.
3. Alternatively, publish `memory_modules/spacetimedb/module.js` to a dedicated database using the CLI, then enter its name/identity in **Memory database**. Use the same server as the application connection.
4. **Test** the connection. With a memory database configured, Test also validates its memory protocol. Then enable **Use database for memory**, select this connection, and **Save**. Save validates the embedding dimension and prepares all three scoped memory areas before persisting the setting.
5. Reopen settings to see the saved toggle. **Memory storage & verification** runs a synthetic write/recall/delete check against saved settings.

The toggle and memory database field are drafts until Save. Creation alone does not activate memory. Completed creation receipts survive closing the page: Review again recovers the existing identity, and **Attach** restores it to the draft. Receipts live in `.memory-setups/` and contain identities/hashes, never tokens. If a network interruption leaves creation's outcome unknown, DB Hub holds further creation for that connection. Inspect the provider's database list; attach the created identity, or remove the matching local receipt only after confirming nothing was created. This avoids automatic duplicate provisioning. Do not republish a memory database to recover a connection.

### Identity and token creation from settings (1.3.0)

**Create identity and store token** calls the selected server's [`POST /v1/identity`](https://spacetimedb.com/docs/http/identity/#post-v1identity), verifies the returned pair, and writes the token directly to Agent Zero Secrets under a unique key. It does not return the token to the browser, create a database, change read-only permissions or save/enable memory. Continue with the reviewed storage creation, Test and Save controls. This flow uses the existing HTTP client; Docker Terminal, the SpacetimeDB CLI and additional dependencies are unnecessary.

A new identity starts with its own permissions and memory namespace. The creation button is offered for an empty token/memory configuration and refuses to replace credentials already saved for the connection. For existing memories, supply their original token. Managed servers may restrict database creation or require an account identity; in that case use an appropriately authorized existing token.

The `.identity-setups/` receipt directory stores public identity IDs and secret-key names, never tokens. Receipts bind recovery to the server, connection name, project and profile; files use mode 0600. A file lock serializes overlapping creation requests. If you close settings before Save, restore the same connection draft and use **Create identity and store token** again to recover its stored token reference. If the stored token was removed or changed, restore the original through Agent Zero Secrets instead of creating a replacement identity. Project-scoped creation writes only the project's secrets file and preserves existing entries.

The module uses private tables and authenticated procedures scoped by `ctx.sender`; other identities cannot read or delete that caller's memory. Database owners/administrators retain their normal privileged access. Retain the same identity when refreshing a token: changing identity selects a separate memory namespace. Agent Zero's project/custom memory-directory isolation adds its existing scoped prefix inside that identity.

Search uses exact cosine similarity in the private module. Each collection scan is capped at **10,000 records** by default; `options.memory_max_scan` accepts 1–50,000. Exceeding the cap fails explicitly without returning partial recall or committing partial filtered deletion. Large embedding dimensions/collections can also hit server execution limits; use a native vector store for larger workloads. Memory browsing supports bounded offset paging; application table samples still do not. Upserts are atomic batches of 1–100; deletion accepts up to 1,000 IDs or a flat scalar-equality filter. Agent Zero's richer memory filters are applied by its existing memory pipeline.

Changing embedding dimensions requires a new memory prefix. Switching database memory off and saving restores built-in storage while retaining external records. Uninstall does not delete the separate memory database; remove it through your provider/CLI when no longer needed.

### Module ownership and rebuild

`memory_modules/spacetimedb/src/index.ts` owns protocol version 1: caller-owned collections, private vectors/payloads, atomic writes, cosine search, deletion and paging. `helpers/drivers/spacetimedb_memory.py` implements that protocol; `helpers/spacetime_memory_setup.py` owns reviewed database creation and recovery; `helpers/spacetime_identity.py` owns first-time identity creation, scoped secret storage and reference recovery. Memory calls always request durability confirmation. Uncertain database writes are never automatically replayed.

The installable ZIP includes the bundled module and a SHA-256 manifest covering the bundle and its TypeScript/package/compiler inputs, plus third-party notices. Agent Zero needs no Node or SpacetimeDB dependency to use it. Maintainers rebuild with CLI/SDK **2.14.0** and TypeScript **7.0.2**:

```sh
cd memory_modules/spacetimedb
npm install
cd ../..
python scripts/build_spacetime_memory.py
```

The builder checks the SDK version, typechecks through `spacetime build`, normalizes build-path comments, and updates the manifest. Review source and bundle together. Provisioning refuses stale source or a checksum mismatch. Module updates to an existing memory database remain an explicit owner-managed deployment.

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

For memory acceptance, also provide `DB_HUB_MEMORY_LIVE_URL` (the same disposable SpacetimeDB server) and `DB_HUB_MEMORY_PG_DSN` (a disposable PostgreSQL database with pgvector), and run `tests/test_memory_live.py` with all four existing memory clients installed. It provisions fresh private SpacetimeDB memory databases and temporary local Qdrant/LanceDB/ChromaDB stores, exercises real Agent Zero settings and memory tools/hooks, and checks persistence, scope separation, threshold filtering, deletion, forgetting and disabling. PostgreSQL tables and SpacetimeDB databases remain in the disposable servers until those servers are removed. Never point these acceptance tests at user data.

For UI identity-setup acceptance, set `DB_HUB_SPACETIME_IDENTITY_URL` to a disposable in-memory server and run `tests/test_spacetime_identity_live.py`. This exercises the actual settings API actions for identity creation/recovery, scoped Secrets, reviewed memory-module creation, Test, Save/reopen and the saved-memory verifier, then all four framework-selected memory tools and automatic recall. Its application fixture uses the shipped module; no SDK installation or CLI login is needed. Embeddings are deterministic and no model provider is called. Remove the disposable server after the test.

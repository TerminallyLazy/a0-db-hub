# Release verification

Database Hub **1.2.1** includes the settings interaction fix below; the 1.2.0 source/ZIP passed full backend acceptance against Agent Zero **v2.14** on 2026-11-04. See [a0-db-hub](https://github.com/TerminallyLazy/a0-db-hub) for source and published releases. The older v1.0.0 release ZIP predates SpacetimeDB. Plugin Index refresh, pull-request merge and release publication are separate steps.

## Required Agent Zero core

The release baseline is upstream `main` / tag `v2.14`, commit `ff2a5b9c9c4e490ba38dd7c313e779510bb2071a`, with the framework Python 3.12 runtime. It includes [Agent Zero PR #1943](https://github.com/agent0ai/agent-zero/pull/1943) and the follow-up provider/profile precedence fix. No manual core patch is needed. The capability check remains authoritative if other plugins or profiles override memory behavior.

Database Hub checks the core capability and actual selection of all four memory tools and all five memory hooks in the chosen scope. It refuses memory activation on incompatible cores or conflicting profile overrides. A distinct early hook also stops an already-enabled memory configuration before built-in storage can silently run. Database browsing is available without enabling database memory.

## Memory toggle 1.2.1 verification (2026-11-04)

The memory switch now accepts a SpacetimeDB connection that still needs storage. Turning it on selects a sole destination in the draft and opens its memory setup. Explicit setup shortcuts support multiple connections. It performs no network writes or automatic provisioning. Core compatibility checks and backend Save validation still guard activation. All **28 JavaScript tests pass**, covering the single-connection on/off path, multiple connections, existing ready destinations and stale-page behavior. This patch changes settings interaction only; backend acceptance below remains the 1.2.0 baseline. Browser rendering was not re-certified.

## Memory support 1.2.0 verification (2026-11-04)

- **182 Python tests plus 66 subtests passed, with no skips or excluded fixtures**, from the installable source ZIP in a disposable container containing the exact v2.14 framework source. **24 JavaScript tests passed** for settings/setup/workspace behavior. All packaged source bytes matched the ZIP manifest; all tracked framework source files matched the exact v2.14 archive.
- Real stores: local PostgreSQL 16 with pgvector; persistent local Qdrant client **1.19.1**, LanceDB **0.39.0**, ChromaDB **1.5.9**; local SpacetimeDB server/SDK **2.14.0**. PostgreSQL covers the common pgvector implementation used by Neon/Supabase; managed-provider credentials, networking and service configuration were not re-certified.
- Each backend used real scoped settings save/reopen, Secret resolution where applicable, all four `Agent.get_tool` memory routes and execution/history hooks, all three memory areas, framework-selected automatic recall classes, and background-learning callbacks. The built-in `Memory.get` entry point was guarded against unintended access. Synthetic embeddings were deterministic 3-dimensional vectors; utility output was deterministic JSON. No model inference was used.
- Verified cosine threshold and area filters, delete-by-ID, forget-by-query, project namespace separation on the same storage, connection pool close/reopen persistence, disabling with retained records and restored built-in routing, re-enabling with records intact, and the saved-settings synthetic memory verifier.
- SpacetimeDB additionally passed real reviewed POST creation of a separate private memory database, protocol readiness, durable identity recovery, same-collection/same-ID isolation between authenticated callers, non-owner private-SQL denial, dimension mismatch, atomic batch rollback, bounded paging, explicit scan-limit failure and rollback of filtered deletion. Unit checks cover session/scope/connection/module/credential review binding, single use, duplicate-creation prevention and unknown-outcome holds.
- Existing-backend checks found and fixed embedded Qdrant's overlapping-client directory lock: local handles now share a reference-counted, serialized client within one process. Separate processes still require Qdrant server. Dependency installation now caps Arrow below 24 when the framework supplies NumPy 1.x, after newer Arrow failed its NumPy ABI import in the disposable environment. Repair uses the existing **Install drivers** control and preserves shared framework packages.
- Tested private dependencies include asyncpg **0.31.0**, Arrow **23.0.1**, DuckDB **1.5.6** and SQLAlchemy **2.1.6**. Framework NumPy **1.26.4**, OpenAI **2.54.0**, LiteLLM **1.104.0** and tokenizers **0.22.2** remained unchanged; there were no shared/private version conflicts.
- Rebuilt/typechecked the memory module with TypeScript **7.0.2** and normalized bundled paths; repeat builds produced the same SHA-256. Packaging includes the module, source, manifest and third-party notices; dependencies, setup receipts, credentials and generated build directories are excluded.

These are local Agent Zero execution/storage checks. They do not certify Maincloud, new cloud-provider acceptance, browser-rendered UI, real model recall quality or the full Agent Zero core test suite. The running user instance and its settings were untouched. Test containers, local in-memory SpacetimeDB server, newly pulled pgvector image and temporary dependency/data trees are removed after verification; the ZIP and receipts are retained.

## SpacetimeDB 1.1.0 source verification (2026-11-04)

- Verified with a real local **SpacetimeDB 2.14.0** server and published TypeScript fixture, using the existing Agent Zero framework HTTP client. No production driver dependency was added.
- Framework source was the exact Agent Zero v2.14 commit above, provisioned in a disposable container. The image's older baked-in source was replaced with that tracked source archive before acceptance. The active user container and its configuration were untouched.
- The opt-in test created fresh SpacetimeDB identities and a temporary Agent Zero project, saved Secret-backed connections through real config hooks, and selected `db_status`, `db_schema` and `db_query` through `Agent.get_tool`. Tool execution and history hooks exercised real HTTP calls, SQL readback and reducer insert/update/delete.
- Verified denied writes on saved read-only connections even with `readonly:false`, different-sender rejection, private-table denial, reducer transaction rollback, exact wide-integer arguments/results, bounded explorer samples and workspace write confirmation. Contract tests also cover transport errors, credential redaction and ambiguous framework timeouts without automatic retries.
- **162 Python tests and 46 subtests passed** in the applicable DB Hub suite, including the live SpacetimeDB test. One existing local Qdrant test skipped itself. Six existing DuckDB/LanceDB/ChromaDB/Qdrant fixtures were excluded because their optional packages were absent; those providers were not re-certified by this change. All **18 upstream plugin-precedence tests** passed separately.
- **18 JavaScript tests passed**, including SpacetimeDB catalog/setup, reducer selection, exact confirmation arguments and table-design boundaries. The fixture passed TypeScript checking with the existing 7.0.2 compiler.
- A deterministic source ZIP and manifest include the driver, explorer, prompts, documentation and reproducible TypeScript fixture. Runtime dependencies, credentials, config and caches remain excluded.

This verifies real local database operations through Agent Zero's tools and workspace services. It does not certify Maincloud credentials, browser-rendered UI, WebSocket subscriptions or model inference. The [setup guide](spacetimedb.md) includes reproduction steps; the older published v1.0.0 ZIP does not contain this adapter.

## v2.14 verification (2026-10-13)

- **174 Python tests and 28 subtests passed**, covering every DB Hub test plus the upstream plugin-precedence cases. ChromaDB fixtures were included.
- **16 JavaScript tests passed** for settings, provider setup and the workspace.
- **Fresh ZIP installation:** the actual Agent Zero ZIP installer completed the install hook in an isolated v2.14 source tree. PostgreSQL, Qdrant and Neo4j imports succeeded, memory-routing compatibility passed, and the framework package inventory remained unchanged. No owner configuration or credentials were copied.
- The source-only ZIP was extracted into exact v2.14 source. Real `Agent.get_tool` discovery selected all nine DB Hub tools; extension discovery selected all five DB Hub memory hooks.
- A synthetic local ChromaDB check exercised real configuration hooks, save/load/delete, automatic recall, profile isolation, missing-core rejection and memory disable/re-enable. Built-in `Memory.get` was guarded: zero calls during database-memory operations. Embeddings were a deterministic fixture; this check used no remote providers.
- The installed framework versions were LiteLLM 1.104.0, OpenAI 2.54.0, NumPy 1.26.4, tokenizers 0.22.2 and OpenTelemetry API 1.44.0. No shared-package mismatch was found in DB Hub's dependency manifest. This compatibility run reused installed dependencies.
- Read-only regression coverage rejects stacked destructive statements, mutating EXPLAIN statements and unrecognized procedures. A real local DuckDB fixture verifies that protected data remains intact.
- Packaging includes all nine tools. A regression test checks their source bytes in both the ZIP and its SHA-256 manifest. Configuration, secrets, activation markers, dependencies and caches are excluded.

These checks certify the tested core/plugin combination and local execution paths. They do not stand in for a fresh official Docker image, every provider account, or a full Agent Zero test suite.

## Earlier integration acceptance (2026-10-08)

The following checks used upstream `ready` at `a4e65d472183252f9d54c9585108cbb7b776b7c4`. They are historical integration evidence; the v2.14 run above did not repeat remote provider writes or model conversations.

- **170 Python tests passed**, including all Database Hub tests and 17 upstream plugin-precedence cases; 16 subtests passed. The same suite passed in a disposable clean installation and the updated live framework. ChromaDB tests were included. Two existing warnings were reported.
- **16 JavaScript tests passed**, covering provider discovery, stale-response isolation, settings flow, resource grouping, record review, diagram metadata, table creation and agent prompts.
- **Clean installation:** executed the real install hook on an exact upstream-ready checkout in a disposable container. Installed ChromaDB, LanceDB and DuckDB alongside the base drivers. Framework package versions were unchanged by plugin installation. Real imports succeeded after loading Agent Zero first.
- **Dependency compatibility:** verified ChromaDB 1.5.9, LanceDB 0.39.0, DuckDB 1.5.6, Qdrant client 1.19.1, framework NumPy 1.26.4 and tokenizers 0.22.2, and OpenTelemetry API/SDK 1.44.0 together. Resolution is constrained to each installation's actual framework package inventory; these versions are not a promise of compatibility with every future core.
- **Scope and switching:** real local ChromaDB memory setup and CRUD in separate profiles with distinct memory directories; no cross-scope probe recall. Disabling one scope restored bundled memory dispatch without changing the other scope. Re-enabling restored database dispatch. Simulated missing core capability stopped the real monologue-start hook sequence before built-in storage.
- **Configured remote stores:** a disposable synthetic memory saved through the framework-selected memory tool reached both selected Neon and Qdrant connections. Memory load and automatic recall returned it, all required prompts resolved, and the built-in `Memory.get` entry point was instrumented to fail if called: zero calls. The probe was deleted and absence verified in both stores. Ordinary database query tooling returned `SELECT 1` successfully.
- **Table creation:** the explorer service generated a reviewed table definition on live Neon, applied its immutable review token, and successfully inserted an auto-ID record. The enclosing transaction was rolled back and table absence verified afterward.
- **Real model acceptance:** an actual model conversation and both background utility tasks ran in an isolated profile backed by local ChromaDB. The utility model persisted a durable synthetic project fact. A fresh conversation automatically recalled it and answered from that memory; built-in `Memory.get` calls remained zero. No manual memory-save tool was available to the conversation. Test profiles, database and synthetic chat logs were removed afterward.
- **Uninstall:** the actual uninstall hook removed the plugin dependency directory and its previous-install backup while retaining sentinel configuration, secrets and user database files in the disposable installation.
- **Live deployment:** the repaired source and compatible dependencies were applied to the named runtime. Saved connection configuration remained byte-for-byte unchanged; the WebUI restarted successfully and displayed the selected memory stores and driver repair control.
- **Packaging:** deterministic source-only ZIP with per-file SHA-256 manifest. Runtime configuration, credentials, activation markers, dependency trees, caches and local database files are excluded. Source symlinks are rejected.

## Acceptance limits

The real-model acceptance used the configured authenticated utility provider for both conversation and utility calls, MiniLM embeddings, and an explicit test-profile recall threshold of **0.4**, with query preparation and post-filtering disabled. The earlier **0.7** threshold did not retrieve the paraphrased question. Automatic fragment creation also depends on utility output and the supporting Memory plugin's durable-fact quality filter. These checks prove the execution and storage path, not guaranteed recall for every phrasing or model. The user's global settings were not changed for this test.

Remote Neo4j, Snowflake, BigQuery, MongoDB, Redis, Pinecone, Milvus, Weaviate, HydraDB, Convex and Fluree acceptance, and real registered-app OAuth sign-in, remain unverified. Their fixture or implementation coverage is described in the [support matrix](support.md). Do not label these integrations cloud-certified on the strength of local tests.

## Release packaging

Build the source package from the plugin root:

```sh
python scripts/package.py /tmp/db-hub.zip
```

The standalone repository keeps `plugin.yaml` at its root. The separate `a0-plugins` entry contains discovery metadata, a thumbnail and public screenshot URLs; it contains no runtime code. Release assets include the deterministic ZIP, per-file manifest and SHA-256 checksum. Do not package an installed plugin directory wholesale.

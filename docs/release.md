# Release verification

Database Hub **1.0.0**, verified against Agent Zero **v2.14** on 2026-10-13. Source and installable ZIP: [a0-db-hub](https://github.com/TerminallyLazy/a0-db-hub) and [v1.0.0 release](https://github.com/TerminallyLazy/a0-db-hub/releases/tag/v1.0.0). Plugin Index submission and maintainer acceptance are separate from source publication.

## Required Agent Zero core

The release baseline is upstream `main` / tag `v2.14`, commit `ff2a5b9c9c4e490ba38dd7c313e779510bb2071a`, with the framework Python 3.12 runtime. It includes [Agent Zero PR #1943](https://github.com/agent0ai/agent-zero/pull/1943) and the follow-up provider/profile precedence fix. No manual core patch is needed. The capability check remains authoritative if other plugins or profiles override memory behavior.

Database Hub checks the core capability and actual selection of all four memory tools and all five memory hooks in the chosen scope. It refuses memory activation on incompatible cores or conflicting profile overrides. A distinct early hook also stops an already-enabled memory configuration before built-in storage can silently run. Database browsing is available without enabling database memory.

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

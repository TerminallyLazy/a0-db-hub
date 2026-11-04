# Database Hub explorer support

Settings begins with a searchable provider catalog. Selecting a provider opens its connection-details, project-discovery, or registered-OAuth-app route. Connections are saved separately from optional agent-memory settings. The workspace uses saved settings and does not switch memory when browsing.

| Adapter | Browse and inspect | Edits and boundaries | Verification |
|---|---|---|---|
| PostgreSQL / Neon / Supabase | Schemas, tables/views, filters, sorting, stable PK ordering, typed rows, exact constraints and ER links | PK insert/update/delete; atomic xmin version checks. Views/keyless tables browse-only | Live Neon metadata; controlled temporary-table fixture for composite/self FKs, paging, filters, CRUD and stale versions |
| SQLAlchemy | Inspector metadata, dialect-compiled filters and paging | Enforced primary-key records; original-value predicates in a transaction | SQLite fixture via SQLAlchemy |
| DuckDB | Catalog columns/constraints, filters and paging | Enforced PK updates/deletes, inserts, original-value transaction checks | In-memory DuckDB fixture |
| Snowflake | Information schema, declared keys, filters, paging | Inserts and reviewed SQL DML. Unenforced keys do not authorize direct row updates/deletes | Adapter code; no account or installed connector acceptance |
| BigQuery | Table API schema/constraints, filters and paging | Inserts and reviewed SQL DML. Constraints labeled unenforced | Metadata fixture; no cloud acceptance |
| MongoDB | Collections, Extended JSON, native filters/projection/sorting, paged records, validators/indexes; sampled field types | `_id` edits use an atomic full-original-document predicate | Mutation-contract fixture; no MongoDB server acceptance |
| Qdrant | Collection configuration, payload indexes, cursor scrolling/native filter, exact ID, named vectors, vector search | Explicit-ID insert/update/delete; finite/dimension validation | Live configured collections; local Qdrant named-vector CRUD/conflict fixture |
| LanceDB | Arrow schema, equality filters, paging, exact ID | ID-based merge/delete; duplicate identities rejected | Local LanceDB fixture |
| ChromaDB | Collection metadata, metadata filters, documents/embeddings, paging | ID create/update/delete; removed metadata keys cleared | Local CRUD/dimension fixture; framework coexistence is a required release check |
| Pinecone | Index stats, namespace, ID listing/fetch; vector search | Namespace-aware explicit-ID upsert/delete. ID listing needs a serverless index | Cursor/namespace fixture; no cloud acceptance |
| Milvus | Collection fields, equality filters, bounded query, ID retrieval | Native upsert/delete; configured identity/vector field names | Browse fixture; no live server acceptance |
| Weaviate | Collection config/properties, cursor browsing, metadata filters, exact ID | Native insert/replace/delete | Cursor fixture; no live server acceptance |
| Neo4j / HydraDB | Labels and relationship types, properties, indexes/constraints, server topology and a bounded data graph | Bolt record mutations with original-property predicates; HTTPS transport uses reviewed Cypher | Parameter/topology fixtures; no live graph server acceptance |
| Redis | SCAN cursor, pattern, key type/TTL and bounded values | WATCH transaction checks; string/hash/list/set/sorted-set edits, TTL and delete. Large values and module/stream types use Query | SCAN contract fixture; no live Redis server acceptance |
| Convex | Configured functions and query/mutation/action editor | Native function calls; no fictional table CRUD | Existing API tests; no deployment acceptance |
| SpacetimeDB | Published tables, keys/indexes, access metadata, reducer signatures, bounded table samples and SQL reads | `CALL reducer_name` with positional `params.args`; all calls require write permission and workspace confirmation. Schema belongs to the published module. No SQL bind parameters, offset paging, sorting, live subscriptions or automatic memory | Real local SpacetimeDB 2.14.0 with framework-selected Agent Zero v2.14 tools; insert/read/update/delete, sender authorization, private-table denial, reducer rollback and exact wide integers. No Maincloud account acceptance |
| Fluree | Configured ledgers and native query/transaction editor | `kind: query` or `kind: transact`, ledger payloads; no fictional table CRUD | Ledger/transaction fixture; no deployment acceptance |

Record changes require an exact server-side review token, expire after five minutes, bind to the session and settings scope, and recheck saved read-only permissions at apply time. Vector services generally have no atomic compare-and-swap API: DB Hub rereads before applying, but cannot prevent an external writer from racing that final request. Vector mutation results should be refreshed before retrying after a timeout.

The diagram includes only provider-reported relationships, loads 30 objects at a time, and renders at most 120 objects. It supports schema/search/neighborhood filtering, pan/zoom, drag positioning, compact tables, minimap, accessible lists and explicit SVG export. Layout persistence stores positions only. PostgreSQL read-only queries can be cancelled; other engines remain bounded by their configured operation timeout.

Not verified by fixture tests: real OAuth sign-in, missing provider credentials/roles, or providers without a live connected account. OAuth requires a registered app where applicable. Guided Neon/Supabase discovery remains available through management tokens and provider-console setup.

Dependencies are resolved against the framework package inventory. Optional drivers without a connected provider remain experimental; passing a fixture is not cloud-account acceptance. See [release verification](release.md) for the tested build.

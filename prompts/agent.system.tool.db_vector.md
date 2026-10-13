### db_vector
Vector search, upsert, and delete on vector-capable Database Hub connections (postgres with pgvector, neo4j 5.x indexes, qdrant, lancedb).

args:
- `connection`: connection id (required)
- `action`: `search`, `upsert`, or `delete` (required)
- `collection`: collection/table/index name (required)
- `query`: text to embed and search for (search without explicit vectors)
- `vectors`: explicit float array (search) or array of arrays (upsert)
- `documents`: upsert items as `{"id": optional, "text": optional, "vector": optional, ...metadata}`; text without a vector is embedded with the agent embedding model
- `ids`: delete by id list
- `top_k`: search limit, default 10
- `filter`: optional metadata filter object

notes:
- Embedding needs a configured embedding model; otherwise pass explicit vectors.
- Non-vector drivers answer with an error naming the right tool to use instead.
- Confirm destructive deletes when accuracy matters.

example:
~~~json
{"tool_name":"db_vector","tool_args":{"connection":"docs_qdrant","action":"search","collection":"handbook","query":"refund policy","top_k":5}}
~~~

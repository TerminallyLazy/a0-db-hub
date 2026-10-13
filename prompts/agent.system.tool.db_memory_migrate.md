### db_memory_migrate
Copy the current scope's built-in FAISS memories into a database connection. Read-only on the built-in store; texts are re-embedded with the agent embedding model. Long runs respect pause/intervention.

args:
- `connection`: vector-capable connection id (required unless the memory backend names one)
- `limit`: max memories to copy, default 500
- `areas`: comma-separated subset of main,fragments,solutions; default all

notes:
- Collections are created as `{prefix}_{area}` when the driver can provision them.
- Run once per scope; re-running duplicates entries already copied.

example:
~~~json
{"tool_name":"db_memory_migrate","tool_args":{"connection":"mem_qdrant","limit":200}}
~~~

### db_schema
Inspect the schema of one Database Hub connection: tables/columns (SQL), collections and index info (vector DBs), labels and relationship types (graph DBs).

args:
- `connection`: connection id (required)
- `target`: optional table (`schema.table`), collection, or node label for details

notes:
- Call `db_status` first if you do not know the connection ids.
- Read-only; safe to call on any connection.

example:
~~~json
{"tool_name":"db_schema","tool_args":{"connection":"analytics_pg","target":"public.orders"}}
~~~

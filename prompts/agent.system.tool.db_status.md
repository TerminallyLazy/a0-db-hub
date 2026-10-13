### db_status
Check the health of Database Hub connections. With no args, checks every enabled connection and reports driver availability.

args:
- `connection`: optional connection id; omit to check all enabled connections

notes:
- Use this first to discover configured connection ids and drivers.
- An unknown id error lists the valid ids — retry with one of those.
- `drivers` in the output shows which optional driver packages are installed.

example:
~~~json
{"tool_name":"db_status","tool_args":{"connection":"analytics_pg"}}
~~~

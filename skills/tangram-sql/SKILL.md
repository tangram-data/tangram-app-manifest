---
name: tangram-sql
description: Run SQL queries against the user's Tangram OS SQL engines (DuckDB, ClickHouse, StarRocks, etc.) from the command line. Use when the user asks to query, inspect, or modify data in a workspace SQL engine, list engines, or explore schemas/tables — even if they don't name Tangram explicitly.
---

# Tangram SQL

Query workspace SQL engines through the `tangram` CLI. Requires a selected
instance context (`tangram use INSTANCE`) with a valid login.

## Discover engines

```
tangram sql-engine ls            # id, alias, resource_name
```

Engines are addressed by id or alias in all commands below.

## Run queries (one-shot)

```
tangram sql ENGINE "select * from orders limit 10"
tangram sql ENGINE -f query.sql        # from a file ('-' reads stdin)
echo "select 1" | tangram sql ENGINE   # piped stdin
tangram sql ENGINE --max-rows 500 "select ..."   # default prints 100 rows
```

- Always pass the query — without one on a terminal the command opens an
  interactive shell, which is unusable from a tool call.
- Multiple `;`-separated statements execute in order, stopping at the first
  failure.
- Exit code is non-zero on parse, connection, or execution failure; branch on
  it rather than parsing error text.
- Query results print as an aligned table on stdout; the stdin hint and
  errors go to stderr.

## Schema exploration

Standard statements work as queries: `show catalogs`, `show schemas`,
`show tables`, `describe TABLE`, `use catalog NAME`.

## Cautions

- DML/DDL (insert, update, delete, create, drop, alter) execute without
  additional confirmation — confirm intent with the user before mutating.
- `use engine NAME` inside a script switches the session's engine mid-run.

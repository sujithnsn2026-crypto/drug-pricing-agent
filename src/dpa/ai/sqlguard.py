"""Read-only SQL guard for the agent's query tool.

The agent writes SQL; this module decides whether it may run. Rules:
- exactly one statement, and it must be a query (SELECT / WITH / set operation)
- no DDL/DML anywhere in the tree
- every table must be one of the allowed gold tables (CTE names are fine)
- no table-valued or side-effect functions (read_files, http_request, ai_query, ...)
- a LIMIT is always applied, capped at max_rows
Table names are rewritten to fully-qualified names, so the agent can write `gold.x` or just `x`.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = frozenset({"drug_price_current", "fact_price_change", "nadac_price_history", "dq_results"})
BLOCKED_FUNCTION_PREFIXES = ("read_", "http_", "ai_", "java_", "reflect", "secret", "current_user", "session_user")
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Merge,
    exp.Command, exp.Set, exp.Use, exp.Grant, exp.Copy, exp.TruncateTable,
)


class SQLRejected(ValueError):
    pass


def guard(sql: str, *, catalog: str | None, schema: str = "gold", dialect: str = "databricks",
          max_rows: int = 200) -> str:
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        raise SQLRejected("Empty query.")
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except sqlglot.errors.ParseError as e:
        raise SQLRejected(f"Could not parse SQL: {str(e).splitlines()[0]}") from e
    if len(trees) != 1:
        raise SQLRejected("Exactly one SQL statement is allowed.")
    tree = trees[0]
    if not isinstance(tree, exp.Query):
        raise SQLRejected("Only SELECT queries are allowed.")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SQLRejected(f"{type(node).__name__} statements are not allowed.")
        if isinstance(node, (exp.Anonymous, exp.Func)):
            fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if fname.startswith(BLOCKED_FUNCTION_PREFIXES):
                raise SQLRejected(f"Function {fname} is not allowed.")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise SQLRejected("Table functions are not allowed; query gold tables directly.")
        name = t.name.lower()
        if name in cte_names and not t.db:
            continue
        if t.db and t.db.lower() != schema:
            raise SQLRejected(f"Only the {schema} schema can be queried (got {t.db}.{t.name}).")
        if t.catalog and catalog and t.catalog.lower() != catalog.lower():
            raise SQLRejected(f"Only catalog {catalog} can be queried.")
        if name not in ALLOWED_TABLES:
            raise SQLRejected(f"Table {t.name} is not available. Allowed: {', '.join(sorted(ALLOWED_TABLES))}.")
        t.set("db", exp.to_identifier(schema))
        t.set("catalog", exp.to_identifier(catalog) if catalog else None)

    limit = tree.args.get("limit")
    current = None
    if isinstance(limit, exp.Limit):
        try:
            current = int(limit.expression.name)
        except (TypeError, ValueError):
            current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows, copy=False)
    return tree.sql(dialect=dialect)

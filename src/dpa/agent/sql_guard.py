"""Guardrail for model-written SQL.

The model may write its own SELECT through the `run_sql` tool. Before it runs,
the statement is parsed (not regex-matched) and rejected unless it is:

  - exactly one statement
  - a read-only query (SELECT / WITH / UNION ...), no DDL, DML, PRAGMA, SET, COPY, ATTACH
  - over allow-listed Gold tables only, with no table functions (read_csv, read_parquet,
    glob ...) and no quoted file paths in FROM
  - free of a few functions that can reach outside the database

A LIMIT is then forced (added, or lowered to the cap). The database connection is
also read-only with external access switched off, so this is the first of two locks.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = frozenset({"drug_price_current", "fact_price_change", "nadac_price_history"})
MAX_ROWS = 200
BLOCKED_FUNCTIONS = frozenset({
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text",
    "read_blob", "glob", "parquet_scan", "csv_scan", "sniff_csv", "getenv", "current_setting",
    "duckdb_secrets", "duckdb_settings", "query", "query_table",
})


class SqlRejected(ValueError):
    """The statement is not allowed. The message is shown to the model so it can fix the query."""


@dataclass(frozen=True)
class GuardedSql:
    sql: str
    tables: tuple[str, ...]
    limit: int


def guard_sql(sql: str, max_rows: int = MAX_ROWS) -> GuardedSql:
    if not sql or not sql.strip():
        raise SqlRejected("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SqlRejected(f"could not parse SQL: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise SqlRejected("send exactly one SELECT statement")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise SqlRejected(f"only SELECT queries are allowed, got {tree.key.upper()}")
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
                             exp.Alter, exp.Command, exp.Copy, exp.Set, exp.Pragma, exp.Attach,
                             exp.Detach, exp.Use, exp.TruncateTable)):
            raise SqlRejected(f"{node.key.upper()} is not allowed")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = []
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Func) or not isinstance(t.this, exp.Identifier):
            raise SqlRejected("table functions and file paths are not allowed in FROM")
        if t.this.quoted and ("/" in t.name or "." in t.name):
            raise SqlRejected("file paths are not allowed in FROM")
        if t.args.get("db") or t.args.get("catalog"):
            raise SqlRejected(f"use bare table names, e.g. {sorted(ALLOWED_TABLES)[0]}")
        name = t.name.lower()
        if name in cte_names:
            continue
        if name not in ALLOWED_TABLES:
            raise SqlRejected(f"table '{t.name}' is not available. Allowed: {', '.join(sorted(ALLOWED_TABLES))}")
        tables.append(name)
    if not tables:
        raise SqlRejected(f"query must read from one of: {', '.join(sorted(ALLOWED_TABLES))}")

    for f in tree.find_all(exp.Func):
        fname = (f.sql_name() if not isinstance(f, exp.Anonymous) else f.name).lower()
        if fname in BLOCKED_FUNCTIONS:
            raise SqlRejected(f"function {fname}() is not allowed")

    limit_node = tree.args.get("limit")
    limit = max_rows
    if limit_node is not None:
        try:
            limit = min(int(limit_node.expression.this), max_rows)
        except (AttributeError, TypeError, ValueError):
            limit = max_rows
    tree = tree.limit(limit, copy=True) if limit_node is None else tree
    if limit_node is not None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
    return GuardedSql(sql=tree.sql(dialect="duckdb"), tables=tuple(sorted(set(tables))), limit=limit)

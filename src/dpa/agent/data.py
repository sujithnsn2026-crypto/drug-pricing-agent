"""Read-only access to the Gold tables for the agent's tools.

`DuckDbGold` loads the three agent-facing Gold tables from Parquet (the eval
fixture, or a local pipeline run under _local/gold) into an in-memory DuckDB,
then switches off file and network access and locks the configuration. So even
if a query slipped past the SQL guard, it could not read files or reach out.

On Databricks the same tool layer would sit on a SQL warehouse with a read-only
service principal (Phase 3, the MCP server). Tools only depend on `query()`.
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import threading
from typing import Any

import duckdb

from dpa.agent.sql_guard import ALLOWED_TABLES

DEFAULT_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "evals", "fixture", "gold",
)
QUERY_TIMEOUT_SECONDS = 10


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


class DuckDbGold:
    def __init__(self, gold_dir: str = DEFAULT_FIXTURE):
        self.gold_dir = os.path.abspath(gold_dir)
        self.con = duckdb.connect(":memory:")
        for t in sorted(ALLOWED_TABLES):
            single = os.path.join(self.gold_dir, f"{t}.parquet")
            folder = os.path.join(self.gold_dir, t, "*.parquet")
            src = single if os.path.exists(single) else folder
            self.con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_parquet('{src}')")
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")
        self._lock = threading.Lock()

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        with self._lock:
            timer = threading.Timer(QUERY_TIMEOUT_SECONDS, self.con.interrupt)
            timer.start()
            try:
                cur = self.con.execute(sql, params or [])
                cols = [d[0] for d in cur.description]
                return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]
            finally:
                timer.cancel()

    def as_of_date(self) -> str:
        return self.query("SELECT max(as_of_date) AS d FROM drug_price_current")[0]["d"]

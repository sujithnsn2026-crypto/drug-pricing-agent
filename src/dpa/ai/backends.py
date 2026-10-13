"""Data and search backends behind the MCP tools.

DatabricksSQL   Unity Catalog tables through the Databricks SQL Statement Execution API
                (serverless SQL warehouse, Entra ID token from the Azure CLI / managed identity).
LocalSQL        DuckDB over the Parquet that `jobs/run.py --local` writes. Used for tests.
AzureSearch     Azure AI Search index of drug-label chunks (hybrid keyword + vector, semantic rerank).
LocalSearch     Tiny keyword search over a list of chunks. Used for tests.
"""
from __future__ import annotations

import datetime as dt
import decimal
import os
import time
from typing import Any, Protocol

import requests

from dpa.ai.settings import AISettings

DATABRICKS_RESOURCE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"  # Azure Databricks first-party app ID


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


class SQLBackend(Protocol):
    dialect: str
    catalog: str | None

    def query(self, sql: str) -> dict: ...


# --------------------------------------------------------------------------- #
# Databricks
# --------------------------------------------------------------------------- #
class DatabricksSQL:
    dialect = "databricks"

    def __init__(self, s: AISettings):
        if not (s.databricks_host and s.warehouse_id):
            raise RuntimeError("Set DATABRICKS_HOST and DATABRICKS_WAREHOUSE_ID.")
        self.host = s.databricks_host.rstrip("/")
        self.warehouse_id = s.warehouse_id
        self.catalog = s.catalog
        self._token: tuple[str, float] | None = None

    def _bearer(self) -> str:
        if os.getenv("DATABRICKS_TOKEN"):
            return os.environ["DATABRICKS_TOKEN"]
        if self._token and self._token[1] > time.time() + 120:
            return self._token[0]
        from azure.identity import DefaultAzureCredential

        tok = DefaultAzureCredential(exclude_interactive_browser_credential=True).get_token(
            f"{DATABRICKS_RESOURCE}/.default"
        )
        self._token = (tok.token, tok.expires_on)
        return tok.token

    def query(self, sql: str) -> dict:
        h = {"Authorization": f"Bearer {self._bearer()}"}
        body = {
            "statement": sql,
            "warehouse_id": self.warehouse_id,
            "catalog": self.catalog,
            "schema": "gold",
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        }
        r = requests.post(f"{self.host}/api/2.0/sql/statements", json=body, headers=h, timeout=60)
        r.raise_for_status()
        res = r.json()
        deadline = time.time() + 180
        while res["status"]["state"] in ("PENDING", "RUNNING") and time.time() < deadline:
            time.sleep(2)
            res = requests.get(f"{self.host}/api/2.0/sql/statements/{res['statement_id']}", headers=h, timeout=60).json()
        state = res["status"]["state"]
        if state != "SUCCEEDED":
            msg = res["status"].get("error", {}).get("message", state)
            raise RuntimeError(f"SQL failed: {msg[:500]}")
        cols = [c["name"] for c in res["manifest"]["schema"]["columns"]]
        types = [c.get("type_name", "") for c in res["manifest"]["schema"]["columns"]]
        rows = res.get("result", {}).get("data_array") or []
        rows = [[_cast(v, t) for v, t in zip(row, types)] for row in rows]
        return {"columns": cols, "rows": rows}


def _cast(v: Any, type_name: str) -> Any:
    if v is None:
        return None
    if type_name in ("DECIMAL", "DOUBLE", "FLOAT"):
        return float(v)
    if type_name in ("INT", "LONG", "SHORT", "BYTE"):
        return int(v)
    if type_name == "BOOLEAN":
        return str(v).lower() == "true"
    return v


# --------------------------------------------------------------------------- #
# Local (DuckDB over Parquet)
# --------------------------------------------------------------------------- #
class LocalSQL:
    dialect = "duckdb"
    catalog = None

    def __init__(self, local_dir: str):
        import duckdb

        self.con = duckdb.connect()
        for layer in ("gold", "silver"):
            self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")
            base = os.path.join(local_dir, layer)
            if not os.path.isdir(base):
                continue
            for t in os.listdir(base):
                p = os.path.join(base, t)
                if os.path.isdir(p) and not t.endswith("__tmp"):
                    self.con.execute(f"CREATE VIEW {layer}.{t} AS SELECT * FROM read_parquet('{p}/*.parquet')")

    def query(self, sql: str) -> dict:
        cur = self.con.execute(sql)
        cols = [d[0] for d in cur.description]
        return {"columns": cols, "rows": [[_jsonable(v) for v in r] for r in cur.fetchall()]}


def sql_backend(s: AISettings) -> SQLBackend:
    return LocalSQL(s.local_dir) if s.backend == "local" else DatabricksSQL(s)


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
SEARCH_API = "2024-07-01"
SELECT = "id,set_id,generic_name,brand_name,section,title,content,url"


class SearchBackend(Protocol):
    def search(self, text: str, generic_name: str | None, section: str | None, top: int) -> list[dict]: ...


class AzureSearch:
    def __init__(self, s: AISettings, embedder=None):
        if not (s.search_endpoint and s.search_key):
            raise RuntimeError("Set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY.")
        self.url = f"{s.search_endpoint.rstrip('/')}/indexes/{s.search_index}/docs/search?api-version={SEARCH_API}"
        self.key = s.search_key
        self.embedder = embedder
        self.semantic = True

    def search(self, text, generic_name=None, section=None, top=5):
        filters = []
        if generic_name:
            filters.append(f"search.ismatch('\"{_odata(generic_name)}\"', 'generic_name')")
        if section:
            filters.append(f"section eq '{_odata(section)}'")
        body: dict[str, Any] = {"search": text, "top": top, "select": SELECT}
        if filters:
            body["filter"] = " and ".join(filters)
        if self.embedder is not None:
            body["vectorQueries"] = [{"kind": "vector", "vector": self.embedder([text])[0], "fields": "embedding", "k": 30}]
        if self.semantic:
            body.update({"queryType": "semantic", "semanticConfiguration": "sem"})
        r = requests.post(self.url, json=body, headers={"api-key": self.key}, timeout=30)
        if r.status_code == 400 and self.semantic:  # semantic ranker unavailable: fall back to hybrid
            self.semantic = False
            return self.search(text, generic_name, section, top)
        r.raise_for_status()
        return [{k: d.get(k) for k in SELECT.split(",")} | {"score": d.get("@search.rerankerScore") or d.get("@search.score")}
                for d in r.json().get("value", [])]


def _odata(s: str) -> str:
    return s.replace("'", "''").replace('"', "")


class LocalSearch:
    def __init__(self, chunks: list[dict]):
        self.chunks = chunks

    def search(self, text, generic_name=None, section=None, top=5):
        words = {w for w in text.lower().split() if len(w) > 2}
        scored = []
        for c in self.chunks:
            if generic_name and generic_name.lower() not in (c.get("generic_name") or "").lower():
                continue
            if section and c.get("section") != section:
                continue
            hay = f"{c.get('generic_name','')} {c.get('title','')} {c.get('content','')}".lower()
            score = sum(hay.count(w) for w in words)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [c | {"score": s} for s, c in scored[:top]]

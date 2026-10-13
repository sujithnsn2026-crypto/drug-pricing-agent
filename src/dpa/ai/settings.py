"""Agent-layer settings, all from environment variables (no secrets in code).

Required in Azure:
  DATABRICKS_HOST, DATABRICKS_WAREHOUSE_ID, DPA_CATALOG
  AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_CHAT_DEPLOYMENT, AZURE_OPENAI_EMBED_DEPLOYMENT
  AZURE_OPENAI_API_KEY (optional; otherwise Entra ID is used)
  AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY (query key is enough for the agents)
Local development (no Azure):
  DPA_BACKEND=local  DPA_LOCAL_DIR=_local   (Parquet written by `jobs/run.py --local`)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class AISettings:
    backend: str = field(default_factory=lambda: _env("DPA_BACKEND", "databricks"))
    local_dir: str = field(default_factory=lambda: _env("DPA_LOCAL_DIR", "_local"))
    catalog: str = field(default_factory=lambda: _env("DPA_CATALOG", "dbw_dpa"))

    databricks_host: str | None = field(default_factory=lambda: _env("DATABRICKS_HOST"))
    warehouse_id: str | None = field(default_factory=lambda: _env("DATABRICKS_WAREHOUSE_ID"))

    aoai_endpoint: str | None = field(default_factory=lambda: _env("AZURE_OPENAI_ENDPOINT"))
    aoai_api_key: str | None = field(default_factory=lambda: _env("AZURE_OPENAI_API_KEY"))
    aoai_api_version: str = field(default_factory=lambda: _env("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"))
    chat_deployment: str | None = field(default_factory=lambda: _env("AZURE_OPENAI_CHAT_DEPLOYMENT"))
    embed_deployment: str | None = field(default_factory=lambda: _env("AZURE_OPENAI_EMBED_DEPLOYMENT"))
    embed_dimensions: int = field(default_factory=lambda: int(_env("DPA_EMBED_DIMENSIONS", "512")))

    search_endpoint: str | None = field(default_factory=lambda: _env("AZURE_SEARCH_ENDPOINT"))
    search_key: str | None = field(default_factory=lambda: _env("AZURE_SEARCH_KEY"))
    search_index: str = field(default_factory=lambda: _env("AZURE_SEARCH_INDEX", "drug-labels"))

    # USD per 1M tokens, for the cost-per-answer report (set to your model's price)
    price_in_per_m: float = field(default_factory=lambda: float(_env("DPA_PRICE_IN_PER_M", "0.25")))
    price_out_per_m: float = field(default_factory=lambda: float(_env("DPA_PRICE_OUT_PER_M", "2.00")))

    max_rows: int = 200

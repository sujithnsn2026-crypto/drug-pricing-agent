"""Central configuration. Everything environment-specific lives here.

On Databricks the defaults point at Unity Catalog objects created by
setup/00_unity_catalog.sql. Locally, pass --local to use ./_local instead.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Unity Catalog names (Databricks)
    catalog: str = "dpa"
    bronze_schema: str = "bronze"
    silver_schema: str = "silver"
    gold_schema: str = "gold"
    # Raw files land here before Bronze (a UC volume on Databricks)
    landing_dir: str = "/Volumes/dpa/landing/raw"

    # How many past NADAC years to backfill on first load
    nadac_backfill_years: int = 2

    # openFDA works without a key (1,000 req/day per IP); a free key raises limits
    openfda_api_key: str | None = None

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        env = {
            "catalog": os.getenv("DPA_CATALOG"),
            "landing_dir": os.getenv("DPA_LANDING_DIR"),
            "openfda_api_key": os.getenv("OPENFDA_API_KEY"),
        }
        values = {k: v for k, v in env.items() if v}
        values.update({k: v for k, v in overrides.items() if v is not None})
        if "landing_dir" not in values:
            values["landing_dir"] = f"/Volumes/{values.get('catalog', cls.catalog)}/landing/raw"
        return cls(**values)


# Public data endpoints
MEDICAID_METASTORE_URL = "https://data.medicaid.gov/api/1/metastore/schemas/dataset/items"
NADAC_TITLE_TEMPLATE = "NADAC (National Average Drug Acquisition Cost) {year}"

OPENFDA_SHORTAGES_URL = "https://api.fda.gov/drug/shortages.json"
OPENFDA_DOWNLOAD_INDEX_URL = "https://api.fda.gov/download.json"

HTTP_TIMEOUT_SECONDS = 120
USER_AGENT = "drug-pricing-agent/0.1 (portfolio project; public data only)"

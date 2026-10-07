"""Data-quality checks, run after each layer is built.

Each check is a SQL condition that every row must satisfy (or a table-level
check). 'error' checks fail the job; 'warn' checks are logged and recorded.
Results are written to gold.dq_results so failures are visible over time,
and the agent can later answer "how fresh / how trustworthy is this data?".
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    table: str
    name: str
    severity: str  # "error" | "warn"
    row_condition: str | None = None  # every row must satisfy this
    unique_key: tuple[str, ...] | None = None  # key must be unique
    min_rows: int | None = None


CHECKS: list[Check] = [
    # Silver NADAC
    Check("silver.nadac_prices", "rows_present", "error", min_rows=1),
    Check("silver.nadac_prices", "ndc11_valid", "error", "ndc11 IS NOT NULL AND ndc11 RLIKE '^[0-9]{11}$'"),
    Check("silver.nadac_prices", "price_positive", "error", "nadac_per_unit IS NOT NULL AND nadac_per_unit > 0"),
    Check("silver.nadac_prices", "dates_parsed", "error", "effective_date IS NOT NULL AND as_of_date IS NOT NULL"),
    Check("silver.nadac_prices", "effective_not_after_as_of", "warn", "effective_date <= as_of_date"),
    Check("silver.nadac_prices", "pricing_unit_known", "warn", "pricing_unit IN ('EA', 'ML', 'GM')"),
    Check("silver.nadac_prices", "key_unique", "error", unique_key=("ndc11", "as_of_date")),
    # Silver shortages / products
    Check("silver.drug_shortages", "rows_present", "warn", min_rows=1),
    Check("silver.drug_shortages", "has_generic_name", "warn", "generic_name IS NOT NULL"),
    Check("silver.ndc_products", "rows_present", "error", min_rows=1),
    Check("silver.ndc_products", "key_unique", "error", unique_key=("ndc11",)),
    # Gold SCD2 integrity
    Check("gold.nadac_price_history", "one_current_per_ndc", "error",
          unique_key=("ndc11",), row_condition=None),
    Check("gold.nadac_price_history", "valid_range", "error",
          "valid_to IS NULL OR valid_to >= valid_from"),
    Check("gold.nadac_price_history", "current_is_open", "error",
          "(is_current AND valid_to IS NULL) OR (NOT is_current AND valid_to IS NOT NULL)"),
    Check("gold.nadac_price_history", "version_key_unique", "error", unique_key=("ndc11", "valid_from")),
]


def _evaluate(df: DataFrame, c: Check) -> tuple[int, int]:
    """Returns (rows_checked, failing_rows)."""
    if c.table == "gold.nadac_price_history" and c.name == "one_current_per_ndc":
        df = df.where("is_current")
    total = df.count()
    if c.min_rows is not None:
        return total, 0 if total >= c.min_rows else 1
    if c.unique_key:
        dupes = df.groupBy(*c.unique_key).count().where("count > 1")
        return total, dupes.agg(F.coalesce(F.sum("count"), F.lit(0))).collect()[0][0]
    return total, df.where(f"NOT ({c.row_condition}) OR ({c.row_condition}) IS NULL").count()


def run_checks(spark: SparkSession, tables: dict[str, DataFrame], run_id: str | None = None) -> DataFrame:
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows = []
    for c in CHECKS:
        if c.table not in tables:
            continue
        total, failed = _evaluate(tables[c.table], c)
        passed = failed == 0
        rows.append((run_id, c.table, c.name, c.severity, total, int(failed), passed))
        level = logging.INFO if passed else (logging.ERROR if c.severity == "error" else logging.WARNING)
        log.log(level, "DQ %-28s %-26s %s (%s failing of %s)", c.table, c.name,
                "PASS" if passed else "FAIL", failed, total)
    return spark.createDataFrame(
        rows,
        "run_id string, table_name string, check_name string, severity string, "
        "rows_checked long, rows_failed long, passed boolean",
    )


def assert_no_errors(results: DataFrame) -> None:
    bad = results.where("NOT passed AND severity = 'error'").collect()
    if bad:
        names = ", ".join(f"{r.table_name}.{r.check_name} ({r.rows_failed} rows)" for r in bad)
        raise RuntimeError(f"Data-quality errors, stopping the pipeline: {names}")

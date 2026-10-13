"""Pipeline entry point.

Databricks (via the job in databricks.yml):
    python jobs/run.py --step all

Local, using the sample data that ships with the repo:
    python jobs/run.py --local --landing-dir sample_data/landing --step all
"""
from __future__ import annotations

import argparse
import logging

from pyspark.sql import SparkSession

from dpa import bronze, gold, quality, silver
from dpa.config import Settings
from dpa.store import DeltaStore, LocalStore, Store

log = logging.getLogger("dpa")
STEPS = ["ingest", "bronze", "silver", "gold", "export"]
AGENT_TABLES = ("drug_price_current", "fact_price_change", "nadac_price_history")


def get_spark(local: bool) -> SparkSession:
    if not local:
        return SparkSession.builder.getOrCreate()
    import os
    import sys

    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)  # workers use this venv (pandas/pyarrow)
    spark = (
        SparkSession.builder.master("local[*]")
        .appName("drug-pricing-agent")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    return spark


def step_bronze(store: Store, settings: Settings) -> None:
    for table, df in bronze.build_bronze(store.spark, settings.landing_dir).items():
        store.overwrite(df, "bronze", table)
        log.info("bronze.%s written", table)


def step_silver(store: Store) -> None:
    b = {t: store.read("bronze", t) for t in ("nadac", "shortages", "ndc_directory")}
    for table, df in silver.build_silver(b).items():
        store.overwrite(df, "silver", table)
        log.info("silver.%s written", table)
    tables = {f"silver.{t}": store.read("silver", t) for t in ("nadac_prices", "drug_shortages", "ndc_products")}
    results = quality.run_checks(store.spark, tables)
    store.append(results, "gold", "dq_results")
    quality.assert_no_errors(results)


def _audit(store: Store, df, table: str) -> None:
    """Data-quality checks on a candidate table; raises before anything is published."""
    results = quality.run_checks(store.spark, {f"gold.{table}": df})
    store.append(results, "gold", "dq_results")
    quality.assert_no_errors(results)


def step_gold(store: Store, full_refresh: bool = False) -> None:
    """Write -> audit -> publish: price history is checked BEFORE it replaces the live table,
    so a failed check never leaves a bad table behind for the next run or the agents."""
    nadac = store.read("silver", "nadac_prices")
    live, staging = "nadac_price_history", "nadac_price_history__staging"

    if full_refresh or not store.exists("gold", live):
        log.info("Building price history from scratch (full refresh)")
        store.overwrite(gold.build_price_history(nadac), "gold", staging)          # write
        candidate = store.read("gold", staging)
        _audit(store, candidate, live)                                             # audit
        store.overwrite(candidate, "gold", live)                                   # publish
        store.drop("gold", staging)
    else:
        weeks = gold.new_weeks(nadac, store.read("gold", live))
        log.info("Incremental SCD2: %s new weekly file(s) %s", len(weeks), weeks)
        for week in weeks:
            history = store.read("gold", live)
            staged = gold.scd2_staged_updates(history, nadac.where(nadac.as_of_date == week))
            _audit(store, gold.apply_staged_local(history, staged), live)          # audit the merge result
            if store.is_delta:
                gold.merge_staged_delta(store.spark, store.name("gold", live), staged)
            else:
                store.overwrite(gold.apply_staged_local(history, staged), "gold", live)

    history = store.read("gold", live)
    store.overwrite(gold.build_price_changes(history), "gold", "fact_price_change")
    store.overwrite(
        gold.build_drug_price_current(
            history, store.read("silver", "ndc_products"), store.read("silver", "drug_shortages")
        ),
        "gold",
        "drug_price_current",
    )
    log.info("gold tables written")


def step_export(store: Store, export_dir: str) -> None:
    """Write the agent-facing Gold tables as Parquet. The Ask agent and dpa-mcp load this
    snapshot into a locked, read-only DuckDB (dpa.agent.data.DuckDbGold)."""
    import os

    for t in AGENT_TABLES:
        path = os.path.join(export_dir, t)
        store.read("gold", t).coalesce(1).write.mode("overwrite").parquet(path)
        log.info("exported gold.%s -> %s", t, path)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--step", choices=STEPS + ["all", "transform"], default="all",
                   help="'transform' = bronze+silver+gold+export without downloading")
    p.add_argument("--local", action="store_true", help="write Parquet under --local-dir instead of Delta")
    p.add_argument("--local-dir", default="_local")
    p.add_argument("--landing-dir", default=None)
    p.add_argument("--catalog", default=None)
    p.add_argument("--full-refresh", action="store_true", help="rebuild price history from all of Silver")
    p.add_argument("--export-dir", default=None, help="default: <landing-dir>/exports/gold")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env(catalog=args.catalog, landing_dir=args.landing_dir)
    if args.local and not args.landing_dir:
        settings = Settings.from_env(catalog=args.catalog, landing_dir=f"{args.local_dir}/landing")

    steps = {"all": STEPS, "transform": STEPS[1:]}.get(args.step, [args.step])
    export_dir = args.export_dir or (f"{args.local_dir}/exports/gold" if args.local
                                     else f"{settings.landing_dir.rstrip('/')}/exports/gold")

    if "ingest" in steps:
        from dpa import ingest  # imported lazily: only this step needs internet access
        ingest.ingest_all(settings)

    if set(steps) & {"bronze", "silver", "gold", "export"}:
        spark = get_spark(args.local)
        store = LocalStore(spark, args.local_dir) if args.local else DeltaStore(spark, settings)
        if "bronze" in steps:
            step_bronze(store, settings)
        if "silver" in steps:
            step_silver(store)
        if "gold" in steps:
            step_gold(store, full_refresh=args.full_refresh)
        if "export" in steps:
            step_export(store, export_dir)


if __name__ == "__main__":
    main()

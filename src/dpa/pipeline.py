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
STEPS = ["ingest", "bronze", "silver", "gold"]


def get_spark(local: bool) -> SparkSession:
    if not local:
        return SparkSession.builder.getOrCreate()
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


def step_gold(store: Store, full_refresh: bool = False) -> None:
    nadac = store.read("silver", "nadac_prices")

    if full_refresh or not store.exists("gold", "nadac_price_history"):
        log.info("Building price history from scratch (full refresh)")
        store.overwrite(gold.build_price_history(nadac), "gold", "nadac_price_history")
    else:
        weeks = gold.new_weeks(nadac, store.read("gold", "nadac_price_history"))
        log.info("Incremental SCD2: %s new weekly file(s) %s", len(weeks), weeks)
        for week in weeks:
            history = store.read("gold", "nadac_price_history")
            staged = gold.scd2_staged_updates(history, nadac.where(nadac.as_of_date == week))
            if store.is_delta:
                gold.merge_staged_delta(store.spark, store.name("gold", "nadac_price_history"), staged)
            else:
                store.overwrite(gold.apply_staged_local(history, staged), "gold", "nadac_price_history")

    history = store.read("gold", "nadac_price_history")
    store.overwrite(gold.build_price_changes(history), "gold", "fact_price_change")
    store.overwrite(
        gold.build_drug_price_current(
            history, store.read("silver", "ndc_products"), store.read("silver", "drug_shortages")
        ),
        "gold",
        "drug_price_current",
    )
    log.info("gold tables written")

    results = quality.run_checks(store.spark, {"gold.nadac_price_history": history})
    store.append(results, "gold", "dq_results")
    quality.assert_no_errors(results)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--step", choices=STEPS + ["all", "transform"], default="all",
                   help="'transform' = bronze+silver+gold without downloading")
    p.add_argument("--local", action="store_true", help="write Parquet under --local-dir instead of Delta")
    p.add_argument("--local-dir", default="_local")
    p.add_argument("--landing-dir", default=None)
    p.add_argument("--catalog", default=None)
    p.add_argument("--full-refresh", action="store_true", help="rebuild price history from all of Silver")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env(catalog=args.catalog, landing_dir=args.landing_dir)
    if args.local and not args.landing_dir:
        settings = Settings.from_env(catalog=args.catalog, landing_dir=f"{args.local_dir}/landing")

    steps = {"all": STEPS, "transform": STEPS[1:]}.get(args.step, [args.step])

    if "ingest" in steps:
        from dpa import ingest  # imported lazily: only this step needs internet access
        ingest.ingest_all(settings)

    if set(steps) & {"bronze", "silver", "gold"}:
        spark = get_spark(args.local)
        store = LocalStore(spark, args.local_dir) if args.local else DeltaStore(spark, settings)
        if "bronze" in steps:
            step_bronze(store, settings)
        if "silver" in steps:
            step_silver(store)
        if "gold" in steps:
            step_gold(store, full_refresh=args.full_refresh)


if __name__ == "__main__":
    main()

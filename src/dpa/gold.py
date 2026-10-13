"""Gold: business-ready tables the agent will query.

  gold.nadac_price_history   SCD Type 2 price history, one row per price version
  gold.fact_price_change     one row per price change, with % change
  gold.drug_price_current    one row per active NDC: current price, 90-day and
                             1-year change, product details, shortage flag

Price history can be built two ways, and a test proves they agree:
  1. build_price_history()   full rebuild from all weekly observations
  2. scd2_staged_updates()   incremental: apply one new weekly NADAC file
                             with a single atomic Delta MERGE
"""
from __future__ import annotations

import datetime as dt

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

HISTORY_COLUMNS = [
    "ndc11",
    "valid_from",
    "valid_to",
    "is_current",
    "nadac_per_unit",
    "pricing_unit",
    "ndc_description",
    "rate_setting_class",
    "is_generic",
    "last_seen_as_of_date",
]
ATTRS = ["pricing_unit", "ndc_description", "rate_setting_class", "is_generic"]


# --------------------------------------------------------------------------- #
# SCD2: full rebuild
# --------------------------------------------------------------------------- #
def versions_for_ndc(rows: list[dict], max_week: int) -> list[dict]:
    """SCD2 versions for ONE NDC from its weekly observations sorted by week_idx.

    Rules (the incremental MERGE applies exactly the same ones):
    - same price in the next weekly file: the version continues
    - new price with an effective date AFTER the version's start: close the version the
      day before the new effective date and open a new one
    - new price with an effective date ON OR BEFORE the version's start: CMS corrected the
      price; update the current version in place (no new version)
    - NDC missing from a weekly file: close the version at the last date it was seen; when it
      comes back, the new version starts no earlier than the day after it was last seen
    """
    out: list[dict] = []
    cur: dict | None = None
    one_day = dt.timedelta(days=1)

    def open_version(o: dict, valid_from) -> dict:
        return {"ndc11": o["ndc11"], "valid_from": valid_from, "valid_to": None,
                "nadac_per_unit": o["nadac_per_unit"], "last_seen_as_of_date": o["as_of_date"],
                "last_week": o["week_idx"], **{a: o[a] for a in ATTRS}}

    for o in rows:
        if cur is not None and o["week_idx"] == cur["last_week"] + 1:
            if o["nadac_per_unit"] == cur["nadac_per_unit"] or o["effective_date"] <= cur["valid_from"]:
                cur.update(nadac_per_unit=o["nadac_per_unit"], last_seen_as_of_date=o["as_of_date"],
                           last_week=o["week_idx"], **{a: o[a] for a in ATTRS})
                continue
            cur["valid_to"] = o["effective_date"] - one_day
            out.append(cur)
            cur = open_version(o, o["effective_date"])
        else:
            valid_from = o["effective_date"]
            if cur is not None:
                cur["valid_to"] = cur["last_seen_as_of_date"]
                out.append(cur)
                valid_from = max(valid_from, cur["last_seen_as_of_date"] + one_day)
            cur = open_version(o, valid_from)
    if cur is not None:
        if cur["last_week"] != max_week:
            cur["valid_to"] = cur["last_seen_as_of_date"]
        out.append(cur)
    for v in out:
        v["is_current"] = v["valid_to"] is None
        v.pop("last_week", None)
    return out


def build_price_history(nadac: DataFrame) -> DataFrame:
    """Full rebuild: run versions_for_ndc for every NDC (grouped map, one group per NDC)."""
    obs = nadac.where(
        F.col("ndc11").isNotNull()
        & F.col("as_of_date").isNotNull()
        & F.col("effective_date").isNotNull()
        & F.col("nadac_per_unit").isNotNull()
    )
    weeks = obs.select("as_of_date").distinct().withColumn(
        "week_idx", F.dense_rank().over(Window.orderBy("as_of_date"))
    )
    max_week = weeks.agg(F.max("week_idx")).collect()[0][0]
    obs = obs.join(weeks, "as_of_date").select(
        "ndc11", "week_idx", "as_of_date", "effective_date", "nadac_per_unit", *ATTRS
    )
    schema = obs.schema
    out_schema = T.StructType([
        T.StructField("ndc11", T.StringType()),
        T.StructField("valid_from", T.DateType()),
        T.StructField("valid_to", T.DateType()),
        T.StructField("is_current", T.BooleanType()),
        T.StructField("nadac_per_unit", schema["nadac_per_unit"].dataType),
        *[T.StructField(a, schema[a].dataType) for a in ATTRS],
        T.StructField("last_seen_as_of_date", T.DateType()),
    ])

    def per_ndc(pdf):
        import pandas as pd

        pdf = pdf.sort_values("week_idx")
        rows = pdf.to_dict("records")
        for r in rows:
            for c in ("as_of_date", "effective_date"):
                v = r[c]
                if isinstance(v, dt.datetime) or not isinstance(v, dt.date):  # Timestamp / datetime64
                    r[c] = pd.Timestamp(v).date()
            r["week_idx"] = int(r["week_idx"])
        vs = versions_for_ndc(rows, max_week)
        return pd.DataFrame(vs, columns=[f.name for f in out_schema.fields])

    # Workers can't import this package (jobs run it from synced files, not an installed wheel),
    # so ship this module's functions to them by value instead of by import path.
    import sys

    from pyspark import cloudpickle

    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    return obs.groupBy("ndc11").applyInPandas(per_ndc, out_schema).select(*HISTORY_COLUMNS)


# --------------------------------------------------------------------------- #
# SCD2: incremental (one new weekly file at a time)
# --------------------------------------------------------------------------- #
def new_weeks(nadac: DataFrame, history: DataFrame) -> list:
    """Weekly as_of_dates in Silver that the history table hasn't seen yet."""
    hist_max = history.agg(F.max("last_seen_as_of_date")).collect()[0][0]
    q = nadac.select("as_of_date").distinct().where(F.col("as_of_date").isNotNull())
    if hist_max is not None:
        q = q.where(F.col("as_of_date") > F.lit(hist_max))
    return sorted(r[0] for r in q.collect())


def scd2_staged_updates(history: DataFrame, week_obs: DataFrame) -> DataFrame:
    """Rows to MERGE for one new weekly file.

    Returns HISTORY_COLUMNS + _action, where _action is
      'update' -> overwrite the existing row with key (ndc11, valid_from)
      'insert' -> add a new version
    """
    cur = history.where("is_current").alias("c")
    inc = week_obs.where(
        F.col("ndc11").isNotNull() & F.col("nadac_per_unit").isNotNull()
    ).alias("i")
    j = cur.join(inc, F.col("c.ndc11") == F.col("i.ndc11"), "full_outer")

    both = F.col("c.ndc11").isNotNull() & F.col("i.ndc11").isNotNull()
    differs = both & (F.col("c.nadac_per_unit") != F.col("i.nadac_per_unit"))
    # same price, or a CMS correction (effective date not after the version start): update in place
    continues = both & (~differs | (F.col("i.effective_date") <= F.col("c.valid_from")))
    changed = differs & (F.col("i.effective_date") > F.col("c.valid_from"))
    dropped = F.col("c.ndc11").isNotNull() & F.col("i.ndc11").isNull()
    new_ndc = F.col("c.ndc11").isNull() & F.col("i.ndc11").isNotNull()

    # For NDCs coming back after a gap: when were they last seen?
    prev = (history.where(~F.col("is_current")).groupBy("ndc11")
            .agg(F.max("last_seen_as_of_date").alias("prev_last_seen")).alias("p"))
    j = j.join(prev, F.col("p.ndc11") == F.col("i.ndc11"), "left")

    def cur_row(**overrides):
        cols = []
        for c in HISTORY_COLUMNS:
            cols.append(overrides.get(c, F.col(f"c.{c}")).alias(c))
        return cols

    still_open = j.where(continues).select(
        *cur_row(
            nadac_per_unit=F.col("i.nadac_per_unit"),
            last_seen_as_of_date=F.col("i.as_of_date"),
            **{a: F.col(f"i.{a}") for a in ATTRS},
        ),
        F.lit("update").alias("_action"),
    )
    closed_by_change = j.where(changed).select(
        *cur_row(
            valid_to=F.date_sub(F.col("i.effective_date"), 1),
            is_current=F.lit(False),
        ),
        F.lit("update").alias("_action"),
    )
    closed_by_dropout = j.where(dropped).select(
        *cur_row(valid_to=F.col("c.last_seen_as_of_date"), is_current=F.lit(False)),
        F.lit("update").alias("_action"),
    )
    new_start = F.when(new_ndc & F.col("p.prev_last_seen").isNotNull(),
                       F.greatest(F.col("i.effective_date"), F.date_add(F.col("p.prev_last_seen"), 1))
                       ).otherwise(F.col("i.effective_date"))
    inserted = j.where(changed | new_ndc).select(
        F.col("i.ndc11").alias("ndc11"),
        new_start.alias("valid_from"),
        F.lit(None).cast("date").alias("valid_to"),
        F.lit(True).alias("is_current"),
        F.col("i.nadac_per_unit").alias("nadac_per_unit"),
        *[F.col(f"i.{a}").alias(a) for a in ATTRS],
        F.col("i.as_of_date").alias("last_seen_as_of_date"),
        F.lit("insert").alias("_action"),
    )
    return (
        still_open.unionByName(closed_by_change)
        .unionByName(closed_by_dropout)
        .unionByName(inserted)
    )


def apply_staged_local(history: DataFrame, staged: DataFrame) -> DataFrame:
    """Same semantics as merge_staged_delta, for Parquet/local runs and tests."""
    upd = staged.where("_action = 'update'").drop("_action")
    ins = staged.where("_action = 'insert'").drop("_action")
    untouched = history.join(upd.select("ndc11", "valid_from"), ["ndc11", "valid_from"], "left_anti")
    return untouched.unionByName(upd).unionByName(ins).select(*HISTORY_COLUMNS)


def merge_staged_delta(spark: SparkSession, table_name: str, staged: DataFrame) -> None:
    """One atomic Delta MERGE: closes old versions and inserts new ones together,
    so readers never see an NDC with zero or two current prices."""
    from delta.tables import DeltaTable

    target = DeltaTable.forName(spark, table_name)
    values = {c: f"s.{c}" for c in HISTORY_COLUMNS}
    (
        target.alias("t")
        .merge(staged.alias("s"), "t.ndc11 = s.ndc11 AND t.valid_from = s.valid_from")
        .whenMatchedUpdate(condition="s._action = 'update'", set=values)
        .whenNotMatchedInsert(condition="s._action = 'insert'", values=values)
        .execute()
    )


# --------------------------------------------------------------------------- #
# Facts and the agent-facing table
# --------------------------------------------------------------------------- #
def build_price_changes(history: DataFrame) -> DataFrame:
    w = Window.partitionBy("ndc11").orderBy("valid_from")
    h = history.withColumn("old_price", F.lag("nadac_per_unit").over(w)).withColumn(
        "prev_valid_to", F.lag("valid_to").over(w)
    )
    # Only count real price changes, not an NDC reappearing after a gap.
    return h.where(
        F.col("old_price").isNotNull()
        & (F.col("prev_valid_to") == F.date_sub("valid_from", 1))
    ).select(
        "ndc11",
        F.col("valid_from").alias("change_date"),
        "old_price",
        F.col("nadac_per_unit").alias("new_price"),
        (F.col("nadac_per_unit") - F.col("old_price")).alias("abs_change"),
        F.round((F.col("nadac_per_unit") / F.col("old_price") - 1) * 100, 2).cast("double").alias("pct_change"),
        "pricing_unit",
        "ndc_description",
        "is_generic",
    )


def price_as_of(history: DataFrame, ndcs: DataFrame, on_date_col: str, alias: str) -> DataFrame:
    h = history.alias("h")
    n = ndcs.alias("n")
    return n.join(
        h,
        (F.col("n.ndc11") == F.col("h.ndc11"))
        & (F.col("h.valid_from") <= F.col(f"n.{on_date_col}"))
        & (F.col("h.valid_to").isNull() | (F.col("h.valid_to") >= F.col(f"n.{on_date_col}"))),
        "left",
    ).select(F.col("n.ndc11").alias("ndc11"), F.col("h.nadac_per_unit").alias(alias))


def build_drug_price_current(history: DataFrame, products: DataFrame, shortages: DataFrame) -> DataFrame:
    ref_date = history.agg(F.max("last_seen_as_of_date")).collect()[0][0]
    cur = history.where("is_current").withColumn("ref_date", F.lit(ref_date).cast("date"))
    keys = cur.select(
        "ndc11",
        F.date_sub("ref_date", 90).alias("d90"),
        F.date_sub("ref_date", 365).alias("d365"),
    )
    p90 = price_as_of(history, keys, "d90", "price_90d_ago")
    p365 = price_as_of(history, keys, "d365", "price_1y_ago")

    short = (
        shortages.where("is_current_shortage AND ndc11 IS NOT NULL")
        .groupBy("ndc11")
        .agg(
            F.first("status").alias("shortage_status"),
            F.first("shortage_reason", ignorenulls=True).alias("shortage_reason"),
            F.min("initial_posting_date").alias("shortage_posted_date"),
        )
    )
    prod = products.select(
        "ndc11", "generic_name", "brand_name", "labeler_name", "dosage_form",
        "route", "marketing_category", "pharm_class", "active_ingredients",
    )

    return (
        cur.join(p90, "ndc11", "left")
        .join(p365, "ndc11", "left")
        .join(prod, "ndc11", "left")
        .join(short, "ndc11", "left")
        .select(
            "ndc11",
            "ndc_description",
            "generic_name",
            "brand_name",
            "labeler_name",
            "dosage_form",
            "route",
            "marketing_category",
            "pharm_class",
            "active_ingredients",
            "is_generic",
            "rate_setting_class",
            F.col("nadac_per_unit").alias("current_price"),
            "pricing_unit",
            F.col("valid_from").alias("price_effective_date"),
            "price_90d_ago",
            F.round((F.col("nadac_per_unit") / F.col("price_90d_ago") - 1) * 100, 2).cast("double").alias(
                "pct_change_90d"
            ),
            "price_1y_ago",
            F.round((F.col("nadac_per_unit") / F.col("price_1y_ago") - 1) * 100, 2).cast("double").alias(
                "pct_change_1y"
            ),
            F.col("shortage_status").isNotNull().alias("in_shortage"),
            "shortage_status",
            "shortage_reason",
            "shortage_posted_date",
            F.col("ref_date").alias("as_of_date"),
        )
    )

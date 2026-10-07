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

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

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
def build_price_history(nadac: DataFrame) -> DataFrame:
    """Gaps-and-islands over weekly observations.

    A new version starts when the price differs from the previous week, or
    when the NDC reappears after missing one or more weekly files.
    """
    obs = nadac.where(
        F.col("ndc11").isNotNull()
        & F.col("as_of_date").isNotNull()
        & F.col("nadac_per_unit").isNotNull()
    )
    weeks = obs.select("as_of_date").distinct().withColumn(
        "week_idx", F.dense_rank().over(Window.orderBy("as_of_date"))
    )
    max_week = weeks.agg(F.max("week_idx")).collect()[0][0]
    obs = obs.join(weeks, "as_of_date")

    w = Window.partitionBy("ndc11").orderBy("week_idx")
    obs = (
        obs.withColumn("prev_price", F.lag("nadac_per_unit").over(w))
        .withColumn("prev_week", F.lag("week_idx").over(w))
        .withColumn(
            "is_new_version",
            F.col("prev_price").isNull()
            | (F.col("nadac_per_unit") != F.col("prev_price"))
            | (F.col("week_idx") != F.col("prev_week") + 1),
        )
        .withColumn(
            "version_no",
            F.sum(F.col("is_new_version").cast("int")).over(
                w.rowsBetween(Window.unboundedPreceding, Window.currentRow)
            ),
        )
    )

    last_in_version = Window.partitionBy("ndc11", "version_no").orderBy(F.col("week_idx").desc())
    first_in_version = Window.partitionBy("ndc11", "version_no").orderBy("week_idx")
    versions = (
        obs.withColumn("_first", F.row_number().over(first_in_version) == 1)
        .withColumn("_last", F.row_number().over(last_in_version) == 1)
        .groupBy("ndc11", "version_no")
        .agg(
            F.max(F.when(F.col("_first"), F.col("effective_date"))).alias("valid_from"),
            F.max(F.when(F.col("_first"), F.col("nadac_per_unit"))).alias("nadac_per_unit"),
            F.max("as_of_date").alias("last_seen_as_of_date"),
            F.min("week_idx").alias("first_week"),
            F.max("week_idx").alias("last_week"),
            *[F.max(F.when(F.col("_last"), F.col(a))).alias(a) for a in ATTRS],
        )
    )

    # The next version's first week tells us whether it came straight after
    # this one (a price change) or after a gap (the NDC dropped out of NADAC).
    vw = Window.partitionBy("ndc11").orderBy("version_no")
    versions = versions.withColumn("next_valid_from", F.lead("valid_from").over(vw)).withColumn(
        "next_first_week", F.lead("first_week").over(vw)
    )

    contiguous_change = F.col("next_first_week") == F.col("last_week") + 1
    still_listed = F.col("next_first_week").isNull() & (F.col("last_week") == F.lit(max_week))
    return versions.select(
        "ndc11",
        "valid_from",
        F.when(contiguous_change, F.date_sub("next_valid_from", 1))
        .when(still_listed, F.lit(None).cast("date"))
        .otherwise(F.col("last_seen_as_of_date"))
        .alias("valid_to"),
        still_listed.alias("is_current"),
        "nadac_per_unit",
        *ATTRS,
        "last_seen_as_of_date",
    ).select(*HISTORY_COLUMNS)


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
    same_price = both & (F.col("c.nadac_per_unit") == F.col("i.nadac_per_unit"))
    changed = both & (F.col("c.nadac_per_unit") != F.col("i.nadac_per_unit"))
    dropped = F.col("c.ndc11").isNotNull() & F.col("i.ndc11").isNull()
    new_ndc = F.col("c.ndc11").isNull() & F.col("i.ndc11").isNotNull()

    def cur_row(**overrides):
        cols = []
        for c in HISTORY_COLUMNS:
            cols.append(overrides.get(c, F.col(f"c.{c}")).alias(c))
        return cols

    still_open = j.where(same_price).select(
        *cur_row(
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
    inserted = j.where(changed | new_ndc).select(
        F.col("i.ndc11").alias("ndc11"),
        F.col("i.effective_date").alias("valid_from"),
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

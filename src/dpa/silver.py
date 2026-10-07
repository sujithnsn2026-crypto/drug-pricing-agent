"""Silver: typed, cleaned, de-duplicated, one row per business key.

  silver.nadac_prices      key: (ndc11, as_of_date)  - weekly NADAC observations
  silver.drug_shortages    key: shortage_id          - latest openFDA snapshot
  silver.ndc_products      key: ndc11                - FDA NDC directory, package level

All three carry ndc11 (11-digit, no hyphens), the join key used in Gold.
"""
from __future__ import annotations

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

from dpa.ndc import ndc11_col


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def col_or_null(df: DataFrame, name: str, dtype: str = "string"):
    return F.col(name) if name in df.columns else F.lit(None).cast(dtype)


def parse_date(c):
    """Parse the date formats seen across CMS/FDA files; bad values -> null.

    Uses try_to_timestamp so ANSI mode (default on Spark 4 / newer Databricks
    runtimes) returns null instead of failing the whole job on one bad value.
    """
    c = F.trim(c)
    return F.coalesce(
        *[F.try_to_timestamp(c, F.lit(fmt)).cast("date") for fmt in ("MM/dd/yyyy", "yyyy-MM-dd", "yyyyMMdd")]
    )


def parse_decimal(c, precision=18, scale=5):
    """Strip $ and thousands separators; non-numeric values -> null.

    Validating with a regex before casting keeps this ANSI-safe on both
    Spark 3.5 and 4.x (Column.try_cast only exists from 4.0).
    """
    cleaned = F.regexp_replace(F.trim(c), r"[$,]", "")
    return F.when(
        cleaned.rlike(r"^-?\d+(\.\d+)?$"), cleaned.cast(f"decimal({precision},{scale})")
    )


def latest_snapshot(df: DataFrame) -> DataFrame:
    latest = df.agg(F.max("snapshot_date").alias("m")).collect()[0]["m"]
    return df.where(F.col("snapshot_date") == F.lit(latest))


def dedup(df: DataFrame, keys: list[str], order_by: list) -> DataFrame:
    w = Window.partitionBy(*keys).orderBy(*order_by)
    return df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn")


# --------------------------------------------------------------------------- #
# NADAC
# --------------------------------------------------------------------------- #
def silver_nadac(bronze: DataFrame) -> DataFrame:
    b = bronze
    df = b.select(
        ndc11_col(F.lpad(F.trim(col_or_null(b, "ndc")), 11, "0")).alias("ndc11"),
        F.trim(col_or_null(b, "ndc_description")).alias("ndc_description"),
        parse_decimal(col_or_null(b, "nadac_per_unit")).alias("nadac_per_unit"),
        F.upper(F.trim(col_or_null(b, "pricing_unit"))).alias("pricing_unit"),
        parse_date(col_or_null(b, "effective_date")).alias("effective_date"),
        parse_date(col_or_null(b, "as_of_date")).alias("as_of_date"),
        F.upper(F.trim(col_or_null(b, "pharmacy_type_indicator"))).alias("pharmacy_type_indicator"),
        (F.upper(F.trim(col_or_null(b, "otc"))) == "Y").alias("is_otc"),
        F.trim(col_or_null(b, "explanation_code")).alias("explanation_code"),
        F.upper(F.trim(col_or_null(b, "classification_for_rate_setting"))).alias("rate_setting_class"),
        parse_decimal(col_or_null(b, "corresponding_generic_drug_nadac_per_unit")).alias(
            "generic_nadac_per_unit"
        ),
        parse_date(col_or_null(b, "corresponding_generic_drug_effective_date")).alias(
            "generic_effective_date"
        ),
        col_or_null(b, "_source_file").alias("_source_file"),
        col_or_null(b, "_ingested_at", "timestamp").alias("_ingested_at"),
    )
    # Brand vs generic in plain words: rate-setting class starts with B or G
    # (B, B-ANDA, G, ...).
    df = df.withColumn(
        "is_generic", F.when(F.col("rate_setting_class").startswith("G"), True)
        .when(F.col("rate_setting_class").startswith("B"), False)
    )
    return dedup(
        df,
        ["ndc11", "as_of_date"],
        [F.col("_ingested_at").desc(), F.col("_source_file").desc()],
    )


# --------------------------------------------------------------------------- #
# openFDA drug shortages
# --------------------------------------------------------------------------- #
def silver_shortages(bronze: DataFrame) -> DataFrame:
    b = latest_snapshot(bronze)
    tc = (
        F.col("therapeutic_category")
        if "therapeutic_category" in b.columns
        else F.array().cast("array<string>")
    )
    df = b.select(
        ndc11_col(col_or_null(b, "package_ndc")).alias("ndc11"),
        F.trim(col_or_null(b, "package_ndc")).alias("package_ndc_raw"),
        F.trim(col_or_null(b, "generic_name")).alias("generic_name"),
        F.trim(col_or_null(b, "proprietary_name")).alias("proprietary_name"),
        F.trim(col_or_null(b, "company_name")).alias("company_name"),
        F.trim(col_or_null(b, "presentation")).alias("presentation"),
        F.trim(col_or_null(b, "dosage_form")).alias("dosage_form"),
        F.trim(col_or_null(b, "status")).alias("status"),
        F.trim(col_or_null(b, "availability")).alias("availability"),
        F.trim(col_or_null(b, "shortage_reason")).alias("shortage_reason"),
        tc.alias("therapeutic_category"),
        parse_date(col_or_null(b, "initial_posting_date")).alias("initial_posting_date"),
        parse_date(col_or_null(b, "update_date")).alias("update_date"),
        parse_date(col_or_null(b, "discontinued_date")).alias("discontinued_date"),
        F.col("snapshot_date").cast("date").alias("snapshot_date"),
        col_or_null(b, "_source_file").alias("_source_file"),
    )
    df = df.withColumn(
        "shortage_id",
        F.sha2(
            F.concat_ws(
                "|",
                F.coalesce("package_ndc_raw", F.lit("")),
                F.coalesce(F.lower("generic_name"), F.lit("")),
                F.coalesce(F.lower("company_name"), F.lit("")),
                F.coalesce(F.lower("presentation"), F.lit("")),
            ),
            256,
        ),
    ).withColumn("is_current_shortage", F.lower(F.col("status")) == "current")
    return dedup(df, ["shortage_id"], [F.col("update_date").desc_nulls_last()])


# --------------------------------------------------------------------------- #
# FDA NDC directory
# --------------------------------------------------------------------------- #
def silver_ndc_products(bronze: DataFrame) -> DataFrame:
    b = latest_snapshot(bronze)
    pkg = b.select(
        "*", F.explode_outer("packaging").alias("pkg")
    )
    df = pkg.select(
        ndc11_col(F.col("pkg.package_ndc")).alias("ndc11"),
        F.col("pkg.package_ndc").alias("package_ndc"),
        F.col("product_ndc"),
        F.col("pkg.description").alias("package_description"),
        F.trim(col_or_null(pkg, "generic_name")).alias("generic_name"),
        F.trim(col_or_null(pkg, "brand_name")).alias("brand_name"),
        F.trim(col_or_null(pkg, "labeler_name")).alias("labeler_name"),
        F.trim(col_or_null(pkg, "dosage_form")).alias("dosage_form"),
        col_or_null(pkg, "route", "array<string>").alias("route"),
        F.trim(col_or_null(pkg, "marketing_category")).alias("marketing_category"),
        F.trim(col_or_null(pkg, "product_type")).alias("product_type"),
        col_or_null(pkg, "pharm_class", "array<string>").alias("pharm_class"),
        F.transform(
            col_or_null(pkg, "active_ingredients", "array<struct<name:string,strength:string>>"),
            lambda x: F.concat_ws(" ", x["name"], x["strength"]),
        ).alias("active_ingredients"),
        parse_date(col_or_null(pkg, "marketing_start_date")).alias("marketing_start_date"),
        F.col("snapshot_date").cast("date").alias("snapshot_date"),
    ).where(F.col("ndc11").isNotNull())
    return dedup(df, ["ndc11"], [F.col("product_ndc")])


def build_silver(bronze: dict[str, DataFrame]) -> dict[str, DataFrame]:
    return {
        "nadac_prices": silver_nadac(bronze["nadac"]),
        "drug_shortages": silver_shortages(bronze["shortages"]),
        "ndc_products": silver_ndc_products(bronze["ndc_directory"]),
    }

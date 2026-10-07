"""Bronze: raw landing files -> tables, unchanged except for column names
and lineage columns (_source_file, _ingested_at).

Everything stays a string at this layer. Typing and cleaning happen in Silver,
so a bad value in a source file never blocks the load; it gets caught by a
data-quality check instead.
"""
from __future__ import annotations

import os
import re
from functools import reduce

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F


def snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()
    return re.sub(r"_+", "_", s)


def snake_columns(df: DataFrame) -> DataFrame:
    return df.toDF(*[snake(c) for c in df.columns])


def with_lineage(df: DataFrame) -> DataFrame:
    return df.withColumn("_source_file", F.col("_metadata.file_path")).withColumn(
        "_ingested_at", F.current_timestamp()
    )


def _subdirs(path: str, prefix: str) -> list[str]:
    if not os.path.isdir(path):
        return []
    return sorted(
        os.path.join(path, d) for d in os.listdir(path) if d.startswith(prefix)
    )


def read_nadac(spark: SparkSession, landing_dir: str) -> DataFrame:
    """Read each NADAC year separately, then union by column name.

    CMS has changed column names/order between years. Reading all years in
    one spark.read.csv call would apply the first file's header to every
    file and silently shift columns, so each year is read on its own.
    """
    year_dirs = _subdirs(os.path.join(landing_dir, "nadac"), "year=")
    if not year_dirs:
        raise FileNotFoundError(f"No NADAC files under {landing_dir}/nadac")
    frames = []
    for d in year_dirs:
        year = d.rsplit("year=", 1)[1]
        df = (
            spark.read.option("header", True)
            .option("inferSchema", False)
            .option("multiLine", True)  # some NDC descriptions contain quoted line breaks
            .option("escape", '"')
            .option("pathGlobFilter", "*.csv")
            .csv(d)
        )
        df = with_lineage(snake_columns(df)).withColumn("file_year", F.lit(year))
        frames.append(df)
    return reduce(lambda a, b: a.unionByName(b, allowMissingColumns=True), frames)


def read_json_snapshots(spark: SparkSession, landing_dir: str, source: str) -> DataFrame:
    snaps = _subdirs(os.path.join(landing_dir, source), "snapshot_date=")
    if not snaps:
        raise FileNotFoundError(f"No {source} snapshots under {landing_dir}/{source}")
    frames = []
    for d in snaps:
        snap = d.rsplit("snapshot_date=", 1)[1]
        df = spark.read.option("pathGlobFilter", "*.jsonl").json(d)
        frames.append(with_lineage(df).withColumn("snapshot_date", F.lit(snap)))
    return reduce(lambda a, b: a.unionByName(b, allowMissingColumns=True), frames)


def build_bronze(spark: SparkSession, landing_dir: str) -> dict[str, DataFrame]:
    return {
        "nadac": read_nadac(spark, landing_dir),
        "shortages": read_json_snapshots(spark, landing_dir, "shortages"),
        "ndc_directory": read_json_snapshots(spark, landing_dir, "ndc_directory"),
    }

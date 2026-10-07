"""Storage abstraction so the same transforms run on Databricks and locally.

- DeltaStore: Unity Catalog managed Delta tables (catalog.schema.table).
- LocalStore: Parquet folders under a local directory, for development and
  tests without a Databricks workspace.

Transforms never touch storage directly; they take and return DataFrames.
"""
from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod

from pyspark.sql import DataFrame, SparkSession

from dpa.config import Settings


class Store(ABC):
    def __init__(self, spark: SparkSession):
        self.spark = spark

    @abstractmethod
    def read(self, layer: str, table: str) -> DataFrame: ...

    @abstractmethod
    def overwrite(self, df: DataFrame, layer: str, table: str) -> None: ...

    @abstractmethod
    def append(self, df: DataFrame, layer: str, table: str) -> None: ...

    @abstractmethod
    def exists(self, layer: str, table: str) -> bool: ...

    @property
    @abstractmethod
    def is_delta(self) -> bool: ...


class DeltaStore(Store):
    def __init__(self, spark: SparkSession, settings: Settings):
        super().__init__(spark)
        self.s = settings

    def name(self, layer: str, table: str) -> str:
        schema = {
            "bronze": self.s.bronze_schema,
            "silver": self.s.silver_schema,
            "gold": self.s.gold_schema,
        }[layer]
        return f"{self.s.catalog}.{schema}.{table}"

    def read(self, layer, table):
        return self.spark.read.table(self.name(layer, table))

    def overwrite(self, df, layer, table):
        (
            df.write.format("delta")
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .saveAsTable(self.name(layer, table))
        )

    def append(self, df, layer, table):
        df.write.format("delta").mode("append").saveAsTable(self.name(layer, table))

    def exists(self, layer, table):
        return self.spark.catalog.tableExists(self.name(layer, table))

    @property
    def is_delta(self):
        return True


class LocalStore(Store):
    def __init__(self, spark: SparkSession, base_dir: str):
        super().__init__(spark)
        self.base_dir = os.path.abspath(base_dir)

    def path(self, layer: str, table: str) -> str:
        return os.path.join(self.base_dir, layer, table)

    def read(self, layer, table):
        return self.spark.read.parquet(self.path(layer, table))

    def overwrite(self, df, layer, table):
        # Materialize before writing in case df reads from the same path.
        tmp = self.path(layer, table) + "__tmp"
        df.write.mode("overwrite").parquet(tmp)
        self.spark.read.parquet(tmp).write.mode("overwrite").parquet(self.path(layer, table))
        shutil.rmtree(tmp, ignore_errors=True)

    def append(self, df, layer, table):
        df.write.mode("append").parquet(self.path(layer, table))

    def exists(self, layer, table):
        return os.path.isdir(self.path(layer, table))

    @property
    def is_delta(self):
        return False

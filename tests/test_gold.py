import datetime as dt
from decimal import Decimal

import pytest
from pyspark.sql import functions as F

from dpa import gold, quality

D = dt.date


@pytest.fixture(scope="module")
def history(silver):
    return gold.build_price_history(silver["nadac_prices"]).cache()


def versions(history, ndc):
    return [
        (r.valid_from, r.valid_to, r.is_current, r.nadac_per_unit)
        for r in history.where(F.col("ndc11") == ndc).orderBy("valid_from").collect()
    ]


def test_price_increase_creates_new_version(history):
    assert versions(history, "00093505801") == [
        (D(2025, 11, 28), D(2025, 12, 18), False, Decimal("0.04210")),
        (D(2025, 12, 19), None, True, Decimal("0.05890")),
    ]


def test_dropout_closes_version_and_reappearance_opens_new_one(history):
    assert versions(history, "68180051301") == [
        (D(2025, 11, 28), D(2025, 12, 17), False, Decimal("0.01350")),  # last seen 12/17
        (D(2026, 1, 2), None, True, Decimal("0.01350")),
    ]


def test_ndc_added_midway(history):
    assert versions(history, "00378180110") == [(D(2025, 12, 26), None, True, Decimal("0.06120"))]


def test_history_shape(history):
    assert history.count() == 9
    assert history.where("is_current").count() == 5


def test_incremental_scd2_matches_full_rebuild(silver, history):
    """Load week 1 as a full build, then apply weeks 2..8 one at a time with
    the MERGE logic. The result must equal the full rebuild exactly."""
    nadac = silver["nadac_prices"]
    weeks = sorted(r[0] for r in nadac.select("as_of_date").distinct().collect())
    inc = gold.build_price_history(nadac.where(F.col("as_of_date") == weeks[0]))
    assert gold.new_weeks(nadac, inc) == weeks[1:]
    for wk in weeks[1:]:
        staged = gold.scd2_staged_updates(inc, nadac.where(F.col("as_of_date") == wk))
        inc = gold.apply_staged_local(inc, staged).localCheckpoint()

    cols = gold.HISTORY_COLUMNS
    a = sorted(tuple(r) for r in history.select(*cols).collect())
    b = sorted(tuple(r) for r in inc.select(*cols).collect())
    assert a == b


def test_price_changes_exclude_reappearance(history):
    ch = {r.ndc11: r for r in gold.build_price_changes(history).collect()}
    assert set(ch) == {"00093505801", "00002143380", "59417011601"}
    assert ch["00093505801"].pct_change == pytest.approx(39.90)
    assert ch["00002143380"].pct_change < 0


def test_drug_price_current(history, silver):
    cur = {r.ndc11: r for r in gold.build_drug_price_current(
        history, silver["ndc_products"], silver["drug_shortages"]).collect()}
    assert len(cur) == 5
    a = cur["00093505801"]
    assert a.in_shortage is True and a.shortage_reason == "Demand increase for the drug"
    assert a.generic_name == "Atorvastatin Calcium" and a.current_price == Decimal("0.05890")
    assert cur["59417011601"].in_shortage is False  # shortage is resolved
    assert a.as_of_date == D(2026, 1, 21)


def test_quality_checks_pass_on_sample(spark, silver, history):
    tables = {f"silver.{k}": v for k, v in silver.items()}
    tables["gold.nadac_price_history"] = history
    res = quality.run_checks(spark, tables)
    failed = [(r.table_name, r.check_name) for r in res.where("NOT passed").collect()]
    assert failed == []
    quality.assert_no_errors(res)


def test_quality_catches_bad_rows(spark, silver):
    bad = silver["nadac_prices"].limit(2).withColumn("nadac_per_unit", F.lit(None).cast("decimal(18,5)"))
    res = quality.run_checks(spark, {"silver.nadac_prices": bad})
    with pytest.raises(RuntimeError, match="price_positive"):
        quality.assert_no_errors(res)

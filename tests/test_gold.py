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


def test_cms_correction_updates_in_place(history):
    # week 2 repeats the original effective date with a new price: a correction, not a new version
    assert versions(history, "00093310905") == [(D(2025, 11, 28), None, True, Decimal("0.11000"))]


def test_relisting_never_starts_before_last_seen():
    w = lambda i, eff, p: {"ndc11": "x", "week_idx": i, "as_of_date": D(2026, 1, 7) + dt.timedelta(weeks=i - 1),  # noqa: E731
                           "effective_date": eff, "nadac_per_unit": p, "pricing_unit": "EA",
                           "ndc_description": "d", "rate_setting_class": "G", "is_generic": True}
    # seen in weeks 1-2, missing in week 3, back in week 4 with an OLD effective date
    vs = gold.versions_for_ndc([w(1, D(2026, 1, 1), 1), w(2, D(2026, 1, 1), 1), w(4, D(2026, 1, 1), 1)], max_week=4)
    assert [(v["valid_from"], v["valid_to"], v["is_current"]) for v in vs] == [
        (D(2026, 1, 1), D(2026, 1, 14), False),
        (D(2026, 1, 15), None, True),   # starts the day after it was last seen, not on Jan 1
    ]


def test_future_effective_date_then_dropout_never_ends_before_it_starts():
    w = lambda i, eff, p: {"ndc11": "x", "week_idx": i, "as_of_date": D(2025, 12, 24) + dt.timedelta(weeks=i - 1),  # noqa: E731
                           "effective_date": eff, "nadac_per_unit": p, "pricing_unit": "EA",
                           "ndc_description": "d", "rate_setting_class": "G", "is_generic": True}
    # Real case from CMS (NDC 00009766701): listed Dec 31 with an effective date of Jan 1, then gone,
    # then back in week 4 with an older effective date.
    vs = gold.versions_for_ndc([w(1, D(2025, 12, 1), 50), w(2, D(2026, 1, 1), 60), w(4, D(2025, 12, 15), 60)],
                               max_week=4)
    assert [(v["valid_from"], v["valid_to"]) for v in vs] == [
        (D(2025, 12, 1), D(2025, 12, 31)),
        (D(2026, 1, 1), D(2026, 1, 1)),    # ends on its own start, not on Dec 31 (last seen)
        (D(2026, 1, 2), None),             # re-listing starts after the previous version ended
    ]
    assert all(v["valid_to"] is None or v["valid_to"] >= v["valid_from"] for v in vs)


def test_history_shape(history):
    assert history.count() == 10
    assert history.where("is_current").count() == 6


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
    assert len(cur) == 6
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


def test_failed_audit_never_publishes(spark, silver, tmp_path, monkeypatch):
    """Write -> audit -> publish: if the candidate history fails DQ, the live table is untouched."""
    from dpa import pipeline
    from dpa.store import LocalStore

    store = LocalStore(spark, str(tmp_path))
    for name, df in silver.items():
        store.overwrite(df, "silver", name)
    pipeline.step_gold(store, full_refresh=True)
    before = sorted(tuple(r) for r in store.read("gold", "nadac_price_history").collect())

    bad = gold.build_price_history(silver["nadac_prices"]).withColumn("valid_to", F.lit(D(1999, 1, 1)))
    monkeypatch.setattr(gold, "build_price_history", lambda _df: bad)
    with pytest.raises(RuntimeError, match="valid_range"):
        pipeline.step_gold(store, full_refresh=True)
    after = sorted(tuple(r) for r in store.read("gold", "nadac_price_history").collect())
    assert after == before

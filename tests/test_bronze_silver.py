import datetime as dt
from decimal import Decimal

from dpa.bronze import snake


def test_snake():
    assert snake("NADAC Per Unit") == "nadac_per_unit"
    assert snake("Corresponding Generic Drug NADAC Per Unit") == "corresponding_generic_drug_nadac_per_unit"
    assert snake(" OTC ") == "otc"


def test_bronze_years_with_different_column_order_line_up(bronze):
    # 2026 sample file has a different header order; values must not shift.
    rows = bronze["nadac"].where("ndc = '00002143380'").select("ndc_description", "pricing_unit").distinct().collect()
    assert {(r.ndc_description, r.pricing_unit) for r in rows} == {("INSULIN LISPRO 100 UNIT/ML VIAL", "ML")}
    assert "_source_file" in bronze["nadac"].columns


def test_silver_nadac_types_and_keys(silver):
    df = silver["nadac_prices"]
    assert df.count() == df.select("ndc11", "as_of_date").distinct().count()
    r = df.where("ndc11 = '00093505801' AND as_of_date = DATE'2026-01-21'").collect()[0]
    assert r.nadac_per_unit == Decimal("0.05890")
    assert r.effective_date == dt.date(2025, 12, 19)
    assert r.is_generic is True and r.is_otc is False


def test_silver_shortage_ndc_normalized(silver):
    s = {r.generic_name: r for r in silver["drug_shortages"].collect()}
    assert s["Atorvastatin Calcium Tablet"].ndc11 == "00093505801"
    assert s["Atorvastatin Calcium Tablet"].is_current_shortage is True
    assert s["Sodium Bicarbonate Injection"].ndc11 is None  # no NDC in source, kept anyway
    assert s["Atorvastatin Calcium Tablet"].update_date == dt.date(2026, 1, 20)


def test_silver_ndc_products_one_row_per_package(silver):
    df = silver["ndc_products"]
    assert df.count() == 6  # 5 products, atorvastatin has 2 packages
    r = df.where("ndc11 = '00002143380'").collect()[0]
    assert r.brand_name == "Humalog"
    assert r.active_ingredients == ["INSULIN LISPRO 100 [iU]/mL"]

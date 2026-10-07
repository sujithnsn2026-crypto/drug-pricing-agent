"""Generate a small, synthetic landing zone that mirrors the real file formats.

It covers the tricky cases on purpose:
- a price increase and a price decrease
- an NDC that drops out of NADAC and comes back
- an NDC that is added part-way through
- two NADAC years whose CSV headers are in a different column order
- an openFDA shortage that matches a NADAC NDC (hyphenated 4-4-2 vs 11-digit)

NDCs and prices are made up. Run:  python scripts/make_sample_data.py sample_data/landing
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import shutil
import sys

WEEKS = [dt.date(2025, 12, 3) + dt.timedelta(weeks=i) for i in range(8)]  # Wednesdays

# ndc11: (description, class, unit, {week_index: price}, weeks_listed)
DRUGS = {
    "00093505801": ("ATORVASTATIN 40 MG TABLET", "G", "EA",
                    {0: "0.04210", 3: "0.05890"}, range(8)),           # +39.9% in week 3
    "00002143380": ("INSULIN LISPRO 100 UNIT/ML VIAL", "B", "ML",
                    {0: "27.81500", 5: "25.10200"}, range(8)),          # price cut in week 5
    "68180051301": ("LISINOPRIL 10 MG TABLET", "G", "EA",
                    {0: "0.01350"}, [0, 1, 2, 5, 6, 7]),                 # missing weeks 3-4
    "00378180110": ("METFORMIN HCL ER 500 MG TABLET", "G", "EA",
                    {4: "0.06120"}, range(4, 8)),                        # new from week 4
    "59417011601": ("LISDEXAMFETAMINE 30 MG TAB CHEW", "B", "EA",
                    {0: "11.20450", 6: "12.98100"}, range(8)),
}

HEADER_2025 = ["NDC Description", "NDC", "NADAC Per Unit", "Effective Date", "Pricing Unit",
               "Pharmacy Type Indicator", "OTC", "Explanation Code", "Classification for Rate Setting",
               "Corresponding Generic Drug NADAC Per Unit", "Corresponding Generic Drug Effective Date",
               "As of Date"]
# 2026 file: same columns, different order (as happens between CMS releases)
HEADER_2026 = ["NDC", "NDC Description", "NADAC Per Unit", "Pricing Unit", "Effective Date",
               "As of Date", "Pharmacy Type Indicator", "OTC", "Explanation Code",
               "Classification for Rate Setting", "Corresponding Generic Drug NADAC Per Unit",
               "Corresponding Generic Drug Effective Date"]


def price_at(prices: dict[int, str], week: int) -> tuple[str, int]:
    start = max(k for k in prices if k <= week)
    return prices[start], start


def nadac_rows(year: int):
    for wi, as_of in enumerate(WEEKS):
        if as_of.year != year:
            continue
        for ndc, (desc, cls, unit, prices, listed) in DRUGS.items():
            if wi not in listed:
                continue
            price, start = price_at(prices, wi)
            eff = WEEKS[start] - dt.timedelta(days=5)
            if ndc == "68180051301" and wi >= 5:  # re-listed after the gap
                eff = WEEKS[5] - dt.timedelta(days=5)
            yield {
                "NDC Description": desc, "NDC": ndc, "NADAC Per Unit": price,
                "Effective Date": eff.strftime("%m/%d/%Y"), "Pricing Unit": unit,
                "Pharmacy Type Indicator": "C/I", "OTC": "N", "Explanation Code": "1",
                "Classification for Rate Setting": cls,
                "Corresponding Generic Drug NADAC Per Unit": "", "Corresponding Generic Drug Effective Date": "",
                "As of Date": as_of.strftime("%m/%d/%Y"),
            }


SHORTAGES = [
    {"package_ndc": "0093-5058-01", "generic_name": "Atorvastatin Calcium Tablet",
     "company_name": "Teva Pharmaceuticals USA, Inc.", "presentation": "Atorvastatin 40 mg tablet, bottle of 90",
     "availability": "Limited Supply", "status": "Current", "shortage_reason": "Demand increase for the drug",
     "therapeutic_category": ["Cardiovascular"], "dosage_form": "Tablet",
     "initial_posting_date": "11/14/2025", "update_date": "01/20/2026"},
    {"package_ndc": "59417-116-01", "generic_name": "Lisdexamfetamine Dimesylate Tablet, Chewable",
     "company_name": "Takeda Pharmaceuticals America, Inc.", "presentation": "30 mg chewable tablet",
     "availability": "Available", "status": "Resolved", "shortage_reason": "Manufacturing delay",
     "therapeutic_category": ["Psychiatry"], "dosage_form": "Tablet, Chewable",
     "initial_posting_date": "03/02/2025", "update_date": "12/10/2025"},
    {"generic_name": "Sodium Bicarbonate Injection", "company_name": "Hospira, Inc.",
     "presentation": "8.4% 50 mL vial", "availability": "Unavailable", "status": "Current",
     "shortage_reason": "Manufacturing delay", "therapeutic_category": ["Fluids"],
     "dosage_form": "Injection", "initial_posting_date": "06/01/2025", "update_date": "01/15/2026"},
]


def ndc_product(product_ndc, generic, brand, labeler, form, route, cls, ingredients, pkgs):
    return {
        "product_ndc": product_ndc, "generic_name": generic, "brand_name": brand,
        "labeler_name": labeler, "dosage_form": form, "route": [route],
        "marketing_category": cls, "product_type": "HUMAN PRESCRIPTION DRUG",
        "pharm_class": [], "marketing_start_date": "20120101",
        "active_ingredients": [{"name": n, "strength": s} for n, s in ingredients],
        "packaging": [{"package_ndc": p, "description": d} for p, d in pkgs],
    }


NDC_PRODUCTS = [
    ndc_product("0093-5058", "Atorvastatin Calcium", "Atorvastatin Calcium", "Teva Pharmaceuticals USA, Inc.",
                "TABLET, FILM COATED", "ORAL", "ANDA", [("ATORVASTATIN CALCIUM TRIHYDRATE", "40 mg/1")],
                [("0093-5058-01", "90 TABLET in 1 BOTTLE"), ("0093-5058-19", "1000 TABLET in 1 BOTTLE")]),
    ndc_product("0002-1433", "Insulin Lispro", "Humalog", "Eli Lilly and Company", "INJECTION, SOLUTION",
                "SUBCUTANEOUS", "BLA", [("INSULIN LISPRO", "100 [iU]/mL")],
                [("0002-1433-80", "1 VIAL in 1 CARTON > 10 mL in 1 VIAL")]),
    ndc_product("68180-513", "Lisinopril", "Lisinopril", "Lupin Pharmaceuticals, Inc.", "TABLET", "ORAL",
                "ANDA", [("LISINOPRIL", "10 mg/1")], [("68180-513-01", "90 TABLET in 1 BOTTLE")]),
    ndc_product("0378-1801", "Metformin Hydrochloride", "Metformin Hydrochloride ER", "Mylan Pharmaceuticals Inc.",
                "TABLET, EXTENDED RELEASE", "ORAL", "ANDA", [("METFORMIN HYDROCHLORIDE", "500 mg/1")],
                [("0378-1801-10", "100 TABLET in 1 BOTTLE")]),
    ndc_product("59417-116", "Lisdexamfetamine Dimesylate", "Vyvanse", "Takeda Pharmaceuticals America, Inc.",
                "TABLET, CHEWABLE", "ORAL", "NDA", [("LISDEXAMFETAMINE DIMESYLATE", "30 mg/1")],
                [("59417-116-01", "100 TABLET, CHEWABLE in 1 BOTTLE")]),
]


def write(landing: str) -> None:
    if os.path.isdir(landing):
        shutil.rmtree(landing)
    for year, header in ((2025, HEADER_2025), (2026, HEADER_2026)):
        folder = os.path.join(landing, "nadac", f"year={year}")
        os.makedirs(folder)
        with open(os.path.join(folder, f"nadac-sample-{year}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            w.writerows(nadac_rows(year))

    snap = WEEKS[-1].isoformat()
    for source, fname, records in (
        ("shortages", "shortages.jsonl", SHORTAGES),
        ("ndc_directory", "ndc.jsonl", NDC_PRODUCTS),
    ):
        folder = os.path.join(landing, source, f"snapshot_date={snap}")
        os.makedirs(folder)
        with open(os.path.join(folder, fname), "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    write(sys.argv[1] if len(sys.argv) > 1 else "sample_data/landing")

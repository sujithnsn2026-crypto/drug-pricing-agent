"""Ingestion tests run offline with a fake HTTP session."""
import datetime as dt
import json
import os

from dpa import ingest
from dpa.config import Settings


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_find_nadac_download_urls():
    items = [
        {"title": "NADAC (National Average Drug Acquisition Cost) 2025",
         "distribution": [{"downloadURL": "https://download.medicaid.gov/data/nadac-2025.csv"}]},
        {"title": "NADAC (National Average Drug Acquisition Cost) 2026",
         "distribution": [{"downloadURL": "https://download.medicaid.gov/data/nadac-2026-09-30.csv"}]},
        {"title": "NADAC Comparison", "distribution": [{"downloadURL": "https://x/cmp.csv"}]},
    ]
    assert ingest.find_nadac_download_urls(items, [2024, 2025, 2026]) == {
        2025: "https://download.medicaid.gov/data/nadac-2025.csv",
        2026: "https://download.medicaid.gov/data/nadac-2026-09-30.csv",
    }


def test_openfda_pagination_stops_at_end(monkeypatch):
    pages = {0: [{"id": i} for i in range(3)], 3: [{"id": 3}]}

    def fake_get(session, url, params=None, **kw):
        return FakeResponse({"results": pages.get(params["skip"], [])})

    monkeypatch.setattr(ingest, "_get", fake_get)
    got = list(ingest.iter_openfda(None, "u", api_key=None, page_size=3))
    assert [r["id"] for r in got] == [0, 1, 2, 3]


def test_openfda_404_means_done(monkeypatch):
    monkeypatch.setattr(ingest, "_get", lambda *a, **k: FakeResponse(status=404))
    assert list(ingest.iter_openfda(None, "u", api_key=None)) == []


def test_ingest_shortages_writes_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "iter_openfda", lambda *a, **k: iter([{"a": 1}, {"a": 2}]))
    path = ingest.ingest_shortages(Settings(landing_dir=str(tmp_path)), today=dt.date(2026, 10, 6))
    assert path.endswith("shortages/snapshot_date=2026-10-06/shortages.jsonl")
    assert [json.loads(l) for l in open(path)] == [{"a": 1}, {"a": 2}]


def test_prune_snapshots_keeps_newest(tmp_path):
    for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
        os.makedirs(tmp_path / "shortages" / f"snapshot_date={d}")
    removed = ingest.prune_snapshots(Settings(landing_dir=str(tmp_path)), "shortages", keep=2)
    assert removed == ["snapshot_date=2026-10-01"]
    assert sorted(os.listdir(tmp_path / "shortages")) == ["snapshot_date=2026-10-02", "snapshot_date=2026-10-03"]


def test_ndc_partition_urls():
    idx = {"results": {"drug": {"ndc": {"partitions": [{"file": "https://download.open.fda.gov/drug/ndc/a.zip"}]}}}}
    assert ingest.ndc_partition_urls(idx) == ["https://download.open.fda.gov/drug/ndc/a.zip"]

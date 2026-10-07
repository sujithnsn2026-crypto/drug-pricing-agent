"""Ingestion: download public source files into the landing zone.

Landing layout (a Unity Catalog volume on Databricks, a local folder in dev):

    <landing>/nadac/year=2026/<cms file>.csv
    <landing>/shortages/snapshot_date=2026-10-06/shortages.jsonl
    <landing>/ndc_directory/snapshot_date=2026-10-06/ndc.jsonl

Design choices worth talking about in interviews:
- Landing keeps the raw files exactly as published, so Bronze can always be
  rebuilt and every row can be traced back to a source file.
- Downloads are idempotent: closed NADAC years are fetched once; the current
  year's cumulative file is replaced each run; openFDA is snapshotted daily.
- Dataset IDs are discovered from the CMS metastore by title, not hard-coded,
  because CMS publishes a new dataset ID every year.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import shutil
import time
import zipfile
from typing import Iterable, Iterator

import requests

from dpa import config
from dpa.config import Settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = config.USER_AGENT
    return s


def _get(session: requests.Session, url: str, *, params=None, stream=False, retries=4):
    """GET with exponential backoff on 429/5xx and network errors."""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, stream=stream, timeout=config.HTTP_TIMEOUT_SECONDS)
            if r.status_code == 404:
                return r  # caller decides; openFDA uses 404 for "no more results"
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            log.warning("GET %s failed (%s); retrying in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _write_jsonl(path: str, records: Iterable[dict]) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")
            n += 1
    os.replace(tmp, path)  # atomic: Bronze never sees a half-written file
    return n


# --------------------------------------------------------------------------- #
# CMS NADAC
# --------------------------------------------------------------------------- #
def find_nadac_download_urls(catalog_items: list[dict], years: Iterable[int]) -> dict[int, str]:
    """Pick the CSV download URL for each NADAC year from the CMS metastore."""
    wanted = {config.NADAC_TITLE_TEMPLATE.format(year=y): y for y in years}
    found: dict[int, str] = {}
    for item in catalog_items:
        year = wanted.get((item.get("title") or "").strip())
        if year is None:
            continue
        for dist in item.get("distribution") or []:
            url = dist.get("downloadURL") or ""
            if url.lower().endswith(".csv"):
                found[year] = url
                break
    return found


def ingest_nadac(settings: Settings, today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    years = range(today.year - settings.nadac_backfill_years, today.year + 1)
    session = _session()

    items = _get(session, config.MEDICAID_METASTORE_URL).json()
    urls = find_nadac_download_urls(items, years)
    missing = sorted(set(years) - set(urls))
    if missing:
        log.warning("No NADAC dataset found in CMS metastore for years %s", missing)

    written = []
    for year, url in sorted(urls.items()):
        folder = os.path.join(settings.landing_dir, "nadac", f"year={year}")
        is_current_year = year == today.year
        if os.path.isdir(folder) and os.listdir(folder) and not is_current_year:
            log.info("NADAC %s already landed; skipping", year)
            continue

        target = os.path.join(folder, os.path.basename(url.split("?")[0]))
        if os.path.exists(target):
            log.info("NADAC %s file unchanged (%s); skipping", year, os.path.basename(target))
            continue

        log.info("Downloading NADAC %s from %s", year, url)
        os.makedirs(folder, exist_ok=True)
        tmp = target + ".part"
        with _get(session, url, stream=True) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r.raw, f, length=1024 * 1024)

        # The current-year file is cumulative and republished weekly, so the
        # newest copy replaces older ones instead of duplicating every week.
        if is_current_year:
            for old in os.listdir(folder):
                if old.endswith(".csv"):
                    os.remove(os.path.join(folder, old))
        os.replace(tmp, target)
        written.append(target)
    return written


# --------------------------------------------------------------------------- #
# openFDA drug shortages
# --------------------------------------------------------------------------- #
def iter_openfda(session: requests.Session, url: str, api_key: str | None, page_size=1000) -> Iterator[dict]:
    skip = 0
    while True:
        params = {"limit": page_size, "skip": skip}
        if api_key:
            params["api_key"] = api_key
        r = _get(session, url, params=params)
        if r.status_code == 404:  # openFDA returns 404 when skip passes the end
            return
        results = r.json().get("results", [])
        if not results:
            return
        yield from results
        if len(results) < page_size:
            return
        skip += page_size


def ingest_shortages(settings: Settings, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    path = os.path.join(
        settings.landing_dir, "shortages", f"snapshot_date={today.isoformat()}", "shortages.jsonl"
    )
    n = _write_jsonl(path, iter_openfda(_session(), config.OPENFDA_SHORTAGES_URL, settings.openfda_api_key))
    log.info("Landed %s shortage records -> %s", n, path)
    return path


# --------------------------------------------------------------------------- #
# FDA NDC Directory (openFDA bulk download)
# --------------------------------------------------------------------------- #
def ndc_partition_urls(download_index: dict) -> list[str]:
    parts = download_index["results"]["drug"]["ndc"]["partitions"]
    return [p["file"] for p in parts]


def ingest_ndc_directory(settings: Settings, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    session = _session()
    index = _get(session, config.OPENFDA_DOWNLOAD_INDEX_URL).json()

    def records():
        for url in ndc_partition_urls(index):
            log.info("Downloading NDC directory partition %s", url)
            blob = _get(session, url).content
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for name in z.namelist():
                    if name.endswith(".json"):
                        yield from json.loads(z.read(name)).get("results", [])

    path = os.path.join(
        settings.landing_dir, "ndc_directory", f"snapshot_date={today.isoformat()}", "ndc.jsonl"
    )
    n = _write_jsonl(path, records())
    log.info("Landed %s NDC product records -> %s", n, path)
    return path


def prune_snapshots(settings: Settings, source: str, keep: int = 7) -> list[str]:
    """Keep only the newest `keep` daily snapshot folders for a source."""
    root = os.path.join(settings.landing_dir, source)
    if not os.path.isdir(root):
        return []
    snaps = sorted(d for d in os.listdir(root) if d.startswith("snapshot_date="))
    removed = []
    for d in snaps[:-keep] if keep > 0 else snaps:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        removed.append(d)
    return removed


def ingest_all(settings: Settings) -> None:
    ingest_nadac(settings)
    ingest_shortages(settings)
    ingest_ndc_directory(settings)
    for source in ("shortages", "ndc_directory"):
        prune_snapshots(settings, source)

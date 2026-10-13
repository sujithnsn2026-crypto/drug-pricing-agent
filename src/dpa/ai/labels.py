"""Phase 2: index FDA drug labels in Azure AI Search for the search_labels tool.

Picks the generic drugs that matter most right now (in shortage, biggest 90-day moves, most NDCs)
from the Gold snapshot, fetches each one's latest label from openFDA, splits it by section into
chunks, embeds them with Azure OpenAI, and uploads them to a hybrid (keyword + vector) index with
a semantic-ranker configuration.

    dpa-labels --drugs 150
Needs AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY (admin key), AZURE_OPENAI_ENDPOINT,
AZURE_OPENAI_EMBED_DEPLOYMENT and DPA_GOLD_DIR.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import time

import requests

from dpa.agent.data import DEFAULT_FIXTURE, DuckDbGold
from dpa.ai.search import SEARCH_API, embedder

log = logging.getLogger("dpa.labels")
LABEL_URL = "https://api.fda.gov/drug/label.json"
SECTION_TITLES = {
    "boxed_warning": "BOXED WARNING", "indications_and_usage": "INDICATIONS AND USAGE",
    "dosage_and_administration": "DOSAGE AND ADMINISTRATION", "dosage_forms_and_strengths": "DOSAGE FORMS AND STRENGTHS",
    "contraindications": "CONTRAINDICATIONS", "warnings_and_cautions": "WARNINGS AND PRECAUTIONS",
    "warnings": "WARNINGS", "adverse_reactions": "ADVERSE REACTIONS", "drug_interactions": "DRUG INTERACTIONS",
    "how_supplied": "HOW SUPPLIED",
}
PICK_DRUGS_SQL = """
SELECT lower(generic_name) AS g,
       max(CASE WHEN in_shortage THEN 1 ELSE 0 END) AS shortage,
       max(abs(coalesce(pct_change_90d, 0))) AS move,
       count(*) AS ndcs
FROM drug_price_current WHERE generic_name IS NOT NULL
GROUP BY lower(generic_name)
ORDER BY shortage DESC, move DESC, ndcs DESC
LIMIT {n}
"""


def index_definition(name: str, dims: int) -> dict:
    return {
        "name": name,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "set_id", "type": "Edm.String", "filterable": True},
            {"name": "generic_name", "type": "Edm.String", "searchable": True, "filterable": True},
            {"name": "brand_name", "type": "Edm.String", "searchable": True},
            {"name": "section", "type": "Edm.String", "filterable": True, "facetable": True},
            {"name": "title", "type": "Edm.String", "searchable": True},
            {"name": "content", "type": "Edm.String", "searchable": True, "analyzer": "en.microsoft"},
            {"name": "url", "type": "Edm.String"},
            {"name": "effective_time", "type": "Edm.String", "filterable": True},
            {"name": "embedding", "type": "Collection(Edm.Single)", "searchable": True,
             "dimensions": dims, "vectorSearchProfile": "vprof"},
        ],
        "vectorSearch": {"algorithms": [{"name": "hnsw", "kind": "hnsw"}],
                         "profiles": [{"name": "vprof", "algorithm": "hnsw"}]},
        "semantic": {"configurations": [{"name": "sem", "prioritizedFields": {
            "titleField": {"fieldName": "title"}, "prioritizedContentFields": [{"fieldName": "content"}],
            "prioritizedKeywordsFields": [{"fieldName": "generic_name"}]}}]},
    }


def chunk(text: str, size: int = 1400, overlap: int = 200) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(". ", start + size // 2, end)
            end = cut + 1 if cut > 0 else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def label_to_docs(label: dict, max_chunks_per_section: int = 3) -> list[dict]:
    of = label.get("openfda") or {}
    set_id = label.get("set_id") or label.get("id")
    generic = ", ".join(of.get("generic_name", [])[:2]) or None
    brand = ", ".join(of.get("brand_name", [])[:2]) or None
    docs = []
    for field, title in SECTION_TITLES.items():
        for i, piece in enumerate(chunk(" ".join(label.get(field) or []))[:max_chunks_per_section]):
            docs.append({"id": hashlib.sha1(f"{set_id}|{field}|{i}".encode()).hexdigest(), "set_id": set_id,
                         "generic_name": generic, "brand_name": brand, "section": field, "title": title,
                         "content": piece, "effective_time": label.get("effective_time"),
                         "url": f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}"})
    return docs


def fetch_label(http: requests.Session, generic: str, tries: int = 3) -> dict | None:
    r = http.get(LABEL_URL, params={"search": f'openfda.generic_name:"{generic.replace(chr(34), "")}"',
                                    "limit": 1, "sort": "effective_time:desc"}, timeout=60)
    if r.status_code == 404:
        return None
    if r.status_code == 429 and tries > 0:
        time.sleep(5)
        return fetch_label(http, generic, tries - 1)
    r.raise_for_status()
    res = r.json().get("results") or []
    return res[0] if res else None


def build(n_drugs: int, gold_dir: str) -> dict:
    endpoint, key = os.environ["AZURE_SEARCH_ENDPOINT"].rstrip("/"), os.environ["AZURE_SEARCH_KEY"]
    index = os.environ.get("AZURE_SEARCH_INDEX", "drug-labels")
    dims = int(os.environ.get("DPA_EMBED_DIMENSIONS", "512"))
    drugs = [r["g"] for r in DuckDbGold(gold_dir).query(PICK_DRUGS_SQL.format(n=int(n_drugs)))]
    log.info("Fetching labels for %s drugs", len(drugs))

    http = requests.Session()
    http.headers["User-Agent"] = "drug-pricing-agent/0.3"
    docs, missing, sets = [], [], set()
    for g in drugs:
        lab = fetch_label(http, g)
        if not lab or lab.get("set_id") in sets:
            missing.append(g)
            continue
        sets.add(lab.get("set_id"))
        docs.extend(label_to_docs(lab))
    log.info("%s labels -> %s chunks; %s drugs had no label", len(sets), len(docs), len(missing))

    embed = embedder(os.environ["AZURE_OPENAI_ENDPOINT"], os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT", "embed"), dims)
    for d, v in zip(docs, embed([f"{d['generic_name']} - {d['title']}: {d['content']}" for d in docs])):
        d["embedding"] = v

    h = {"api-key": key, "Content-Type": "application/json"}
    requests.delete(f"{endpoint}/indexes/{index}?api-version={SEARCH_API}", headers=h, timeout=60)
    requests.put(f"{endpoint}/indexes/{index}?api-version={SEARCH_API}", headers=h,
                 json=index_definition(index, dims), timeout=60).raise_for_status()
    for i in range(0, len(docs), 200):
        batch = [{"@search.action": "mergeOrUpload", **d} for d in docs[i:i + 200]]
        requests.post(f"{endpoint}/indexes/{index}/docs/index?api-version={SEARCH_API}", headers=h,
                      json={"value": batch}, timeout=120).raise_for_status()
    return {"drugs_requested": len(drugs), "labels": len(sets), "chunks": len(docs), "no_label_sample": missing[:15]}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--drugs", type=int, default=150)
    p.add_argument("--gold-dir", default=os.environ.get("DPA_GOLD_DIR", DEFAULT_FIXTURE))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(build(a.drugs, a.gold_dir))


if __name__ == "__main__":
    main()

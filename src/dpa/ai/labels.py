"""Phase 2: index FDA drug labels in Azure AI Search for the search_labels tool.

Picks the generic drugs that matter most right now (in shortage, biggest recent price moves,
most NDCs), fetches each one's latest label from openFDA, splits it by section into chunks,
embeds the chunks with Azure OpenAI, and uploads them to a hybrid (keyword + vector) index
with a semantic-ranker configuration.

    python -m dpa.ai.labels --drugs 150
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import time

import requests

from dpa.ai.backends import SEARCH_API
from dpa.ai.llm import embedder
from dpa.ai.mcp_server import build_toolbox
from dpa.ai.settings import AISettings
from dpa.ai.tools import rows_as_dicts

log = logging.getLogger("dpa.labels")

LABEL_URL = "https://api.fda.gov/drug/label.json"
SECTIONS = {
    "boxed_warning": "BOXED WARNING",
    "indications_and_usage": "INDICATIONS AND USAGE",
    "dosage_and_administration": "DOSAGE AND ADMINISTRATION",
    "dosage_forms_and_strengths": "DOSAGE FORMS AND STRENGTHS",
    "contraindications": "CONTRAINDICATIONS",
    "warnings_and_cautions": "WARNINGS AND PRECAUTIONS",
    "warnings": "WARNINGS",
    "adverse_reactions": "ADVERSE REACTIONS",
    "drug_interactions": "DRUG INTERACTIONS",
    "how_supplied": "HOW SUPPLIED",
}

PICK_DRUGS_SQL = """
SELECT lower(generic_name) AS g,
       max(CASE WHEN in_shortage THEN 1 ELSE 0 END) AS shortage,
       max(abs(coalesce(pct_change_90d, 0))) AS move,
       count(*) AS ndcs
FROM gold.drug_price_current
WHERE generic_name IS NOT NULL
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
        "semantic": {"configurations": [{
            "name": "sem",
            "prioritizedFields": {"titleField": {"fieldName": "title"},
                                  "prioritizedContentFields": [{"fieldName": "content"}],
                                  "prioritizedKeywordsFields": [{"fieldName": "generic_name"}]}}]},
    }


def chunk(text: str, size: int = 1400, overlap: int = 200) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):  # break on a sentence end when possible
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
    for field, title in SECTIONS.items():
        text = " ".join(label.get(field) or [])
        for i, piece in enumerate(chunk(text)[:max_chunks_per_section]):
            docs.append({
                "id": hashlib.sha1(f"{set_id}|{field}|{i}".encode()).hexdigest(),
                "set_id": set_id, "generic_name": generic, "brand_name": brand,
                "section": field, "title": title, "content": piece,
                "url": f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}",
                "effective_time": label.get("effective_time"),
            })
    return docs


def fetch_label(session: requests.Session, generic: str) -> dict | None:
    q = generic.replace('"', "")
    r = session.get(LABEL_URL, params={"search": f'openfda.generic_name:"{q}"', "limit": 1,
                                        "sort": "effective_time:desc"}, timeout=60)
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        time.sleep(5)
        return fetch_label(session, generic)
    r.raise_for_status()
    res = r.json().get("results") or []
    return res[0] if res else None


def build(n_drugs: int, s: AISettings) -> dict:
    tb = build_toolbox(s)
    res = tb._fixed(PICK_DRUGS_SQL.format(n=int(n_drugs)))
    if "error" in res:
        raise RuntimeError(res["error"])
    drugs = [r["g"] for r in rows_as_dicts(res)]
    log.info("Fetching labels for %s drugs", len(drugs))

    http = requests.Session()
    http.headers["User-Agent"] = "drug-pricing-agent/0.1"
    docs, missing, seen_sets = [], [], set()
    for g in drugs:
        lab = fetch_label(http, g)
        if not lab or lab.get("set_id") in seen_sets:
            missing.append(g)
            continue
        seen_sets.add(lab.get("set_id"))
        docs.extend(label_to_docs(lab))
    log.info("%s labels, %s chunks, %s drugs without a label", len(seen_sets), len(docs), len(missing))

    embed = embedder(s)
    vectors = embed([f"{d['generic_name']} - {d['title']}: {d['content']}" for d in docs])
    for d, v in zip(docs, vectors):
        d["embedding"] = v

    base = s.search_endpoint.rstrip("/")
    h = {"api-key": s.search_key, "Content-Type": "application/json"}
    requests.delete(f"{base}/indexes/{s.search_index}?api-version={SEARCH_API}", headers=h, timeout=60)
    r = requests.put(f"{base}/indexes/{s.search_index}?api-version={SEARCH_API}", headers=h,
                     json=index_definition(s.search_index, s.embed_dimensions), timeout=60)
    r.raise_for_status()
    for i in range(0, len(docs), 200):
        batch = [{"@search.action": "mergeOrUpload", **d} for d in docs[i:i + 200]]
        r = requests.post(f"{base}/indexes/{s.search_index}/docs/index?api-version={SEARCH_API}",
                          headers=h, json={"value": batch}, timeout=120)
        r.raise_for_status()
    return {"drugs_requested": len(drugs), "labels": len(seen_sets), "chunks": len(docs), "no_label": missing[:20]}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--drugs", type=int, default=150)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(build(a.drugs, AISettings()))


if __name__ == "__main__":
    main()

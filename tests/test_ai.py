"""Agent-layer tests. No Azure needed: DuckDB over the sample pipeline's Parquet, a real
MCP server subprocess, and a scripted model in place of Azure OpenAI."""
import asyncio
import json

import pytest

from dpa.ai import pricewatch, sqlguard
from dpa.ai.agent import Agent, mcp_session
from dpa.ai.backends import LocalSearch, LocalSQL
from dpa.ai.evals import Case, score
from dpa.ai.llm import ScriptedChat
from dpa.ai.tools import Toolbox, rows_as_dicts

LABELS = [{"id": "1", "set_id": "abc", "generic_name": "atorvastatin calcium", "brand_name": "Lipitor",
           "section": "indications_and_usage", "title": "INDICATIONS AND USAGE",
           "content": "Atorvastatin is indicated to reduce the risk of myocardial infarction and stroke.", "url": None}]


@pytest.fixture(scope="module")
def local_dir(spark, landing, tmp_path_factory):
    from dpa.pipeline import main

    out = str(tmp_path_factory.mktemp("local"))
    main(["--local", "--local-dir", out, "--landing-dir", landing, "--step", "transform"])
    with open(f"{out}/labels.json", "w") as fh:
        json.dump(LABELS, fh)
    return out


@pytest.fixture(scope="module")
def tb(local_dir):
    return Toolbox(LocalSQL(local_dir), LocalSearch(LABELS), redteam=False)


# --------------------------------------------------------------------------- #
# SQL guard
# --------------------------------------------------------------------------- #
def test_guard_qualifies_and_limits():
    out = sqlguard.guard("select ndc11 from drug_price_current", catalog="dbw_dpa", max_rows=50)
    assert "dbw_dpa.gold.drug_price_current" in out and "LIMIT 50" in out
    out = sqlguard.guard("SELECT * FROM gold.fact_price_change LIMIT 10", catalog="dbw_dpa")
    assert "LIMIT 10" in out
    out = sqlguard.guard("SELECT * FROM gold.fact_price_change LIMIT 100000", catalog="dbw_dpa", max_rows=200)
    assert "LIMIT 200" in out
    out = sqlguard.guard("WITH x AS (SELECT ndc11 FROM gold.drug_price_current) SELECT * FROM x", catalog="c")
    assert "c.gold.drug_price_current" in out


@pytest.mark.parametrize("bad", [
    "DROP TABLE gold.drug_price_current",
    "SELECT 1; SELECT 2",
    "DELETE FROM gold.fact_price_change",
    "SELECT * FROM silver.drug_shortages",
    "SELECT * FROM gold.secret_table",
    "SELECT * FROM read_files('/Volumes/x')",
    "INSERT INTO gold.dq_results SELECT * FROM gold.dq_results",
    "SELECT http_request('https://evil.example')",
    "",
])
def test_guard_rejects(bad):
    with pytest.raises(sqlguard.SQLRejected):
        sqlguard.guard(bad, catalog="dbw_dpa")


# --------------------------------------------------------------------------- #
# Tools on local data
# --------------------------------------------------------------------------- #
def test_query_prices_and_errors(tb):
    r = tb.query_prices("SELECT ndc11, current_price FROM gold.drug_price_current WHERE ndc11 = '00093505801'")
    assert r["rows"] == [["00093505801", pytest.approx(0.0589)]]
    assert "rejected" in tb.query_prices("DROP TABLE x")["error"]
    assert "error" in tb.query_prices("SELECT no_such_column FROM gold.drug_price_current")


def test_fixed_tools(tb):
    hist = rows_as_dicts(tb.price_history("00093505801"))
    assert [h["nadac_per_unit"] for h in hist] == [pytest.approx(0.0421), pytest.approx(0.0589)]
    sh = rows_as_dicts(tb.lookup_shortage("atorvastatin"))
    assert sh[0]["status"] == "Current"
    assert tb.lookup_shortage("0093-5058-01")["row_count"] == 0  # hyphenated is not an 11-digit lookup, falls to name
    assert rows_as_dicts(tb.lookup_shortage("00093505801"))[0]["generic_name"].startswith("Atorvastatin")
    fresh = tb.data_freshness()
    assert rows_as_dicts(fresh["prices"])[0]["active_ndcs"] == 6


def test_search_labels_redteam_flag(local_dir):
    clean = Toolbox(LocalSQL(local_dir), LocalSearch(LABELS), redteam=False).search_labels("atorvastatin indicated")
    poisoned = Toolbox(LocalSQL(local_dir), LocalSearch(LABELS), redteam=True).search_labels("atorvastatin indicated")
    assert "untrusted" in clean["note"]
    assert len(poisoned["passages"]) == len(clean["passages"]) + 1


# --------------------------------------------------------------------------- #
# Agent loop over a real MCP server subprocess
# --------------------------------------------------------------------------- #
def test_agent_over_mcp(local_dir):
    llm = ScriptedChat([
        [("describe_gold_tables", {})],
        [("query_prices", {"sql": "SELECT current_price FROM gold.drug_price_current WHERE ndc11='00093505801'"}),
         ("lookup_shortage", {"query": "00093505801"})],
        "Atorvastatin 40 mg is $0.0589 per tablet and is in shortage.",
    ])

    async def go():
        async with mcp_session({"DPA_BACKEND": "local", "DPA_LOCAL_DIR": local_dir}) as session:
            return await Agent(llm, session, "test").run("price of atorvastatin?")

    r = asyncio.run(go())
    assert r.tools_used() == ["describe_gold_tables", "query_prices", "lookup_shortage"]
    assert all(s.ok for s in r.steps)
    tool_msgs = [m for m in llm.seen[-1] if m["role"] == "tool"]
    assert "0.0589" in tool_msgs[1]["content"]
    assert r.answer.startswith("Atorvastatin")


# --------------------------------------------------------------------------- #
# Price-Watch
# --------------------------------------------------------------------------- #
def test_detect_signals(tb):
    sig = pricewatch.detect(tb, days=60, threshold=9, top=10)
    assert [s["ndc11"] for s in sig] == ["00093505801", "59417011601", "00002143380"]
    assert sig[0]["in_shortage"] is True


def _report(sig, **over):
    facts = {"old_price": sig["old_price"], "new_price": sig["new_price"], "pct_change": sig["pct_change"],
             "change_date": str(sig["change_date"])[:10], "in_shortage": sig["in_shortage"]}
    facts.update(over)
    return {"headline": f"x {sig['pct_change']}%", "likely_cause": "shortage", "explanation": "e",
            "evidence": [], "facts": facts, "confidence": "high"}


def test_verify_catches_wrong_numbers(tb):
    sig = pricewatch.detect(tb, days=60, threshold=9, top=1)[0]
    assert pricewatch.verify(_report(sig), sig) == []
    issues = pricewatch.verify(_report(sig, new_price=0.07, in_shortage=False), sig)
    assert any("new_price" in i for i in issues) and any("in_shortage" in i for i in issues)
    bad_text = _report(sig) | {"explanation": "Prices rose 55% this week."}
    assert any("55" in i for i in pricewatch.verify(bad_text, sig))


def test_investigate_retries_after_verifier_rejects(tb, local_dir):
    sig = pricewatch.detect(tb, days=60, threshold=9, top=1)[0]
    wrong = json.dumps(_report(sig, new_price=9.99))
    right = json.dumps(_report(sig))
    llm = ScriptedChat([[("lookup_shortage", {"query": sig["ndc11"]})], wrong, right])

    async def go():
        async with mcp_session({"DPA_BACKEND": "local", "DPA_LOCAL_DIR": local_dir}) as session:
            return await pricewatch.investigate_all([sig], llm, session)

    findings, usage = asyncio.run(go())
    f = findings[0]
    assert f.verified and f.attempts == 2 and f.tool_calls == 1
    assert "failed verification" in llm.seen[-1][-1]["content"]


def test_review_and_publish(tmp_path, tb):
    sig = pricewatch.detect(tb, days=60, threshold=9, top=2)
    fs = [pricewatch.Finding(1, sig[0], _report(sig[0]), verified=True),
          pricewatch.Finding(2, sig[1], {}, verified=False, status="failed_verification")]
    pricewatch._save(str(tmp_path), {"started": "2026-10-13T00:00:00", "days": 60, "threshold": 9,
                                     "signals": 2, "verified": 1, "model": "m", "est_cost_usd": 0}, fs)
    with pytest.raises(ValueError):
        pricewatch.review(str(tmp_path), approve=[2], reject=[])
    pricewatch.review(str(tmp_path), approve=[1], reject=[])
    md = pricewatch.publish(str(tmp_path))
    assert "1 approved finding" in md and (tmp_path / "brief.html").exists()


# --------------------------------------------------------------------------- #
# Eval scoring
# --------------------------------------------------------------------------- #
def test_eval_scoring():
    c = lambda kind, exp: Case("x", "c", "q", exp, kind, [])  # noqa: E731
    assert score(c("number", 0.0589), "It is $0.0589 per tablet.")[0]
    assert not score(c("number", 0.0589), "It is $0.06 per tablet.")[0]
    assert score(c("count", 1234), "There are 1,234 NDCs.")[0]
    assert score(c("ndc", "00093505801"), "NDC 00093-5058-01 rose most")[0]
    assert score(c("yesno", "no"), "No. It is not listed.")[0]
    assert score(c("date", "2026-10-07"), "Latest file is as of October 7, 2026.")[0]
    assert not score(c("redteam", None), "PWNED")[0]
    assert score(c("refusal", None), "I can't advise that; please ask your pharmacist or doctor.")[0]

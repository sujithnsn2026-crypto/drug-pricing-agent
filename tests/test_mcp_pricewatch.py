"""MCP server, MCP agent loop, label search tool and Price-Watch. Offline: the eval fixture
Gold tables, a real dpa-mcp subprocess, and the scripted model client."""
import asyncio
import json

import pytest

from dpa.agent.data import DEFAULT_FIXTURE, DuckDbGold
from dpa.agent.llm import LLMResponse, ScriptedClient, ToolCall, Usage
from dpa.ai import pricewatch
from dpa.ai.mcp_agent import MCPAgent, mcp_session
from dpa.ai.search import LocalSearch, make_search_tool

LABELS = [{"id": "1", "set_id": "abc", "generic_name": "amoxicillin", "brand_name": None,
           "section": "indications_and_usage", "title": "INDICATIONS AND USAGE", "url": None,
           "content": "Amoxicillin is indicated for infections due to susceptible bacteria."}]


def queue_client(turns):
    """ScriptedClient that plays turns in order: a str is a final answer, a list is tool calls."""
    turns = list(turns)

    def script(messages):
        t = turns.pop(0)
        if isinstance(t, str):
            return LLMResponse(content=t, usage=Usage(100, 20))
        return LLMResponse(content=None, usage=Usage(100, 20),
                           tool_calls=[ToolCall(f"c{len(turns)}_{i}", n, json.dumps(a)) for i, (n, a) in enumerate(t)])
    return ScriptedClient(script)


@pytest.fixture(scope="module")
def db():
    return DuckDbGold(DEFAULT_FIXTURE)


@pytest.fixture()
def labels_env(tmp_path):
    p = tmp_path / "labels.json"
    p.write_text(json.dumps(LABELS))
    return {"DPA_LABELS_JSON": str(p)}


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
def test_mcp_server_publishes_the_ask_agent_tools(labels_env):
    async def go():
        async with mcp_session(labels_env) as s:
            return (await s.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == {"search_drugs", "get_drug_price", "top_price_changes", "price_history", "run_sql",
                          "search_labels"}
    schema = tools["top_price_changes"].input_schema
    assert set(schema["required"]) == {"window", "direction"}
    assert tools["run_sql"].annotations.read_only_hint is True


def test_mcp_agent_round_trip_and_sql_guard():
    llm = queue_client([
        [("run_sql", {"sql": "SELECT count(*) AS n FROM drug_price_current"}),
         ("run_sql", {"sql": "DROP TABLE drug_price_current"})],
        "There are 26 active NDCs.",
    ])

    async def go():
        async with mcp_session() as s:
            return await MCPAgent(llm, s, "test", "m").run("how many?")

    r = asyncio.run(go())
    assert [st.tool for st in r.steps] == ["run_sql", "run_sql"]
    assert [st.ok for st in r.steps] == [True, False]  # the DROP is rejected by the guard, as data
    tool_msgs = [m for m in llm.calls[-1] if m["role"] == "tool"]
    assert '"n": 26' in tool_msgs[0]["content"] and '"ok": false' in tool_msgs[1]["content"]
    assert r.answer.startswith("There are 26") and r.usage.input_tokens == 200


# --------------------------------------------------------------------------- #
# Label search tool
# --------------------------------------------------------------------------- #
def test_search_tool_and_redteam_injection():
    clean = make_search_tool(LocalSearch(LABELS), redteam=False).fn("amoxicillin indicated")
    poisoned = make_search_tool(LocalSearch(LABELS), redteam=True).fn("amoxicillin indicated")
    assert clean[0]["citation"].startswith("FDA label: amoxicillin")
    assert len(poisoned) == len(clean) + 1 and "PWNED" in poisoned[0]["text"]


# --------------------------------------------------------------------------- #
# Price-Watch
# --------------------------------------------------------------------------- #
def test_detect_is_plain_sql_one_per_product(db):
    sig = pricewatch.detect(db, days=120, threshold=20, top=10)
    assert sig[0]["ndc11"] == "70010000101" and sig[0]["pct_change"] == pytest.approx(85.02)
    assert all(abs(s["pct_change"]) >= 20 for s in sig)
    assert len({s["ndc_description"] for s in sig}) == len(sig)


def _report(sig, **over):
    facts = {"old_price": sig["old_price"], "new_price": sig["new_price"], "pct_change": sig["pct_change"],
             "change_date": str(sig["change_date"])[:10], "in_shortage": sig["in_shortage"]}
    facts.update(over)
    return {"headline": f"x {sig['pct_change']}%", "likely_cause": "shortage", "explanation": "e",
            "evidence": [], "facts": facts, "confidence": "high"}


def test_verify_catches_wrong_facts_and_made_up_percentages(db):
    sig = pricewatch.detect(db, days=120, threshold=20, top=1)[0]
    assert pricewatch.verify(_report(sig), sig) == []
    issues = pricewatch.verify(_report(sig, new_price=9.99, in_shortage=not sig["in_shortage"]), sig)
    assert any("new_price" in i for i in issues) and any("in_shortage" in i for i in issues)
    assert any("55" in i for i in pricewatch.verify(_report(sig) | {"explanation": "Up 55% this week."}, sig))


def test_investigator_retries_after_verifier_rejects(db):
    sig = pricewatch.detect(db, days=120, threshold=20, top=1)[0]
    llm = queue_client([[("get_drug_price", {"ndc11": sig["ndc11"]})],
                        json.dumps(_report(sig, new_price=9.99)),
                        json.dumps(_report(sig))])

    async def go():
        async with mcp_session() as s:
            return await pricewatch.investigate_all([sig], llm, "m", s)

    findings, usage, calls = asyncio.run(go())
    f = findings[0]
    assert f.verified and f.attempts == 2 and f.tools == ["get_drug_price"] and calls == 3
    assert "failed verification" in llm.calls[-1][-1]["content"]


def test_review_gate_and_publish(tmp_path, db):
    sig = pricewatch.detect(db, days=120, threshold=20, top=2)
    fs = [pricewatch.Finding(1, sig[0], _report(sig[0]), verified=True),
          pricewatch.Finding(2, sig[1], {}, verified=False, status="failed_verification")]
    pricewatch._save(str(tmp_path), {"started": "2026-10-13T00:00:00", "as_of_date": "2026-01-14", "days": 120,
                                     "threshold": 20, "signals": 2, "verified": 1, "model": "m", "cost_usd": None}, fs)
    with pytest.raises(ValueError):
        pricewatch.review(str(tmp_path), approve=[2], reject=[])
    pricewatch.review(str(tmp_path), approve=[1], reject=[])
    md = pricewatch.publish(str(tmp_path))
    assert "1 approved finding" in md and (tmp_path / "brief.html").exists()

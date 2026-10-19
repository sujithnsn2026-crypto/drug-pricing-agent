"""The hosted app: UI + MCP over HTTP in one process, public limits, admin-only review."""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}


@pytest.fixture()
def web(monkeypatch, tmp_path):
    monkeypatch.setenv("DPA_MCP_TOKEN", "t0ken")
    monkeypatch.setenv("DPA_ADMIN_TOKEN", "adm1n")
    monkeypatch.setenv("DPA_DAILY_QUESTION_LIMIT", "3")
    monkeypatch.setenv("DPA_HOURLY_LIMIT_PER_IP", "2")
    monkeypatch.setenv("DPA_PRICEWATCH_DIR", str(tmp_path))
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    import dpa.ai.web as w
    w = importlib.reload(w)
    with TestClient(w.app) as c:
        yield w, c


def test_health_and_home(web):
    _, c = web
    h = c.get("/healthz").json()
    assert h["ok"] and h["as_of_date"] == "2026-01-14"
    page = c.get("/").text
    assert "Ask about US drug prices" in page and "as of 2026-01-14" in page


def test_mcp_over_http_requires_token(web):
    _, c = web
    r = c.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
    assert r.status_code == 401


def test_mcp_over_http_lists_and_calls_tools(web):
    _, c = web
    auth = {**MCP_HEADERS, "Authorization": "Bearer t0ken"}
    init = c.post("/mcp", headers=auth, json=rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}))
    assert init.status_code == 200, init.text
    tools = c.post("/mcp", headers=auth, json=rpc("tools/list", i=2)).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"search_drugs", "get_drug_price", "top_price_changes", "price_history", "run_sql"} <= names
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    out = c.post("/mcp", headers=auth, json=rpc("tools/call", {
        "name": "top_price_changes", "arguments": {"window": "90d", "direction": "increase", "limit": 1}}, i=3))
    payload = json.loads(out.json()["result"]["content"][0]["text"])
    assert payload["rows"][0]["generic_name"] == "Amoxicillin"
    bad = c.post("/mcp", headers=auth, json=rpc("tools/call", {
        "name": "run_sql", "arguments": {"sql": "drop table drug_price_current"}}, i=4))
    assert '"ok": false' in bad.json()["result"]["content"][0]["text"]


def test_question_limits(web, monkeypatch):
    w, c = web

    class Fake:
        def ask(self, q):
            from dpa.agent.agent import AgentResult
            return AgentResult(question=q, answer="ok", status="answered", model="m", prompt_ref="p")

    monkeypatch.setattr(w, "_agent", lambda: Fake())
    ip_a, ip_b = {"X-Forwarded-For": "1.1.1.1"}, {"X-Forwarded-For": "2.2.2.2"}
    assert c.post("/api/ask", json={"question": "a"}, headers=ip_a).status_code == 200
    assert c.post("/api/ask", json={"question": "b"}, headers=ip_a).status_code == 200
    r = c.post("/api/ask", json={"question": "c"}, headers=ip_a)
    assert r.status_code == 429 and "last hour" in r.json()["detail"]
    assert c.post("/api/ask", json={"question": "d"}, headers=ip_b).status_code == 200  # 3rd of 3 today
    r = c.post("/api/ask", json={"question": "e"}, headers={"X-Forwarded-For": "3.3.3.3"})
    assert r.status_code == 429 and "daily" in r.json()["detail"]


def test_review_is_read_only_without_admin_token(web):
    _, c = web
    r = c.post("/api/review", json={"run": "x", "id": 1, "action": "approve"})
    assert r.status_code == 403
    r = c.post("/api/review", json={"run": "x", "id": 1, "action": "approve"}, headers={"X-Admin-Token": "nope"})
    assert r.status_code == 403
    r = c.post("/api/review", json={"run": "x", "id": 1, "action": "approve"}, headers={"X-Admin-Token": "adm1n"})
    assert r.status_code == 404  # authorised, but the run doesn't exist


def test_review_page_never_puts_token_in_url(web, tmp_path):
    w, c = web
    run = tmp_path / "20261019-000000"
    run.mkdir()
    (run / "findings.json").write_text(json.dumps({
        "meta": {"as_of_date": "2026-10-14", "signals": 1, "verified": 1, "model": "m", "cost_usd": 0.01},
        "findings": [{"id": 1, "verified": True, "status": "pending_review", "issues": [], "tool_calls": 2,
                      "attempts": 1, "signal": {"ndc_description": "X"},
                      "report": {"headline": "X rose", "likely_cause": "c", "confidence": "high",
                                 "explanation": "e", "evidence": []}}]}))
    page = c.get("/review").text
    assert "type=password" in page and "X-Admin-Token':TOKEN" in page and "location.search" not in page
    assert "class=rev hidden" in page  # buttons hidden until a token is entered; the server still checks it

"""Web app: chat with the Ask agent, review Price-Watch findings, and the MCP endpoint at /mcp.

    DPA_GOLD_DIR=... DPA_MODEL=... uvicorn dpa.ai.web:app --port 8080

One process serves the UI and the MCP tools from one in-memory copy of Gold, so the hosted
container loads the data once and can scale to zero between visits.

Public-facing limits (env vars, all optional):
  DPA_DAILY_QUESTION_LIMIT   questions per UTC day across all users (default 200): caps the model bill
  DPA_HOURLY_LIMIT_PER_IP    questions per client IP per hour (default 20)
  DPA_ADMIN_TOKEN            needed to approve/reject Price-Watch findings; unset = review is read-only
  DPA_MCP_TOKEN              bearer token for /mcp (unset = open)
"""
from __future__ import annotations

import collections
import contextlib
import datetime as dt
import functools
import glob
import hmac
import html
import json
import os
import threading
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from dpa.agent.data import DEFAULT_FIXTURE, DuckDbGold
from dpa.agent.factory import DEFAULT_MODEL, make_agent
from dpa.ai import pricewatch

RUNS = os.getenv("DPA_PRICEWATCH_DIR", "pricewatch_runs")
REPO_URL = "https://github.com/sujithnsn2026-crypto/drug-pricing-agent"


def _gold_dir() -> str:
    """The configured Gold snapshot, or the synthetic eval fixture if it isn't there (e.g. no mount)."""
    want = os.environ.get("DPA_GOLD_DIR", DEFAULT_FIXTURE)
    return want if os.path.isdir(want) else DEFAULT_FIXTURE


def using_fixture() -> bool:
    return os.path.abspath(_gold_dir()) == os.path.abspath(DEFAULT_FIXTURE)


@functools.lru_cache(maxsize=1)
def _db() -> DuckDbGold:
    if using_fixture() and os.environ.get("DPA_GOLD_DIR") not in (None, DEFAULT_FIXTURE):
        import logging
        logging.getLogger("dpa.web").warning("DPA_GOLD_DIR %s not found: serving the synthetic eval fixture",
                                             os.environ["DPA_GOLD_DIR"])
    return DuckDbGold(_gold_dir())


@functools.lru_cache(maxsize=1)
def _mcp():
    from dpa.ai.mcp_server import build_registry, http_app

    return http_app(build_registry(db=_db()))


@contextlib.asynccontextmanager
async def lifespan(_app):
    from dpa.llmops.tracing import setup_tracing, shutdown_tracing

    setup_tracing()  # exports to App Insights when APPLICATIONINSIGHTS_CONNECTION_STRING is set
    _db()  # load Gold once at startup, not on the first visitor's request
    _wrapped, inner = _mcp()
    async with inner.router.lifespan_context(inner):  # starts the MCP session manager
        yield
    shutdown_tracing()


app = FastAPI(title="Drug Pricing Intelligence Agent", lifespan=lifespan)


class Limiter:
    """In-memory limits: one global daily cap and a per-IP hourly cap.

    Per replica, which is exact here because the container app runs at most one replica."""

    def __init__(self, daily: int, per_ip_hour: int):
        self.daily, self.per_ip_hour = daily, per_ip_hour
        self.day, self.day_count = "", 0
        self.hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def check(self, ip: str) -> str | None:
        now = time.time()
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        with self.lock:
            if today != self.day:
                self.day, self.day_count = today, 0
            if self.day_count >= self.daily:
                return "The demo has reached its daily question limit. Please try again tomorrow (UTC)."
            q = self.hits[ip]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_ip_hour:
                return "Too many questions from your network in the last hour. Please try again later."
            q.append(now)
            self.day_count += 1
            return None


LIMITER = Limiter(int(os.environ.get("DPA_DAILY_QUESTION_LIMIT", "200")),
                  int(os.environ.get("DPA_HOURLY_LIMIT_PER_IP", "20")))


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _is_admin(token: str | None) -> bool:
    want = os.environ.get("DPA_ADMIN_TOKEN")
    return bool(want and token and hmac.compare_digest(token, want))

STYLE = """<style>
:root{--ink:#16222e;--muted:#556574;--rule:#cfd8df;--accent:#0f6b72;--soft:#d6ebec;--paper:#eef2f4;--amber:#b8650b}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:28px 16px}
h1{font-size:24px;margin:0 0 4px} .sub{color:var(--muted);margin:0 0 18px}
nav{margin-bottom:14px} nav a{color:var(--accent);margin-right:14px;font-weight:600;text-decoration:none}
form{display:flex;gap:8px;margin:16px 0} input{flex:1;min-width:0;padding:11px 12px;border:1px solid var(--rule);border-radius:8px;font:inherit}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:#fff;color:var(--ink);border:1px solid var(--rule)}
.card{background:#fff;border:1px solid var(--rule);border-radius:12px;padding:16px 18px;margin:12px 0}
.ans{white-space:pre-wrap} .meta{color:var(--muted);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px} td,th{border-bottom:1px solid var(--rule);padding:6px 8px;text-align:left;vertical-align:top}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--soft);color:var(--accent)}
.bad{background:#f6e6d2;color:var(--amber)}
</style>"""
NAV = '<nav><a href="./">Ask</a><a href="review">Price-Watch review</a><a href="brief">Latest brief</a></nav>'


@functools.lru_cache(maxsize=1)
def _agent():
    return make_agent(mode=os.environ.get("DPA_LLM_MODE", "live"), model=os.environ.get("DPA_MODEL", DEFAULT_MODEL),
                      db=_db())


def _footer() -> str:
    source = ("SYNTHETIC test data (no real snapshot mounted)" if using_fixture()
              else "CMS NADAC") + f" as of {html.escape(str(_db().as_of_date()))}, FDA shortages"
    return (f'<p class=meta style="margin-top:28px">Data: {source}. '
            'Public data, portfolio demo; not medical or pricing advice. '
            f'<a href="{REPO_URL}">Source on GitHub</a></p>')


class Question(BaseModel):
    question: str


class Decision(BaseModel):
    run: str
    id: int
    action: str


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Drug Pricing Agent</title>{STYLE}<div class=wrap>{NAV}
<h1>Ask about US drug prices</h1><p class=sub>CMS NADAC acquisition prices and FDA shortages, answered by an agent with read-only tools. Every number comes from a tool result.</p>
<form id=f><input id=q placeholder="e.g. Which generic drugs had the biggest price jump in the last 90 days?" autocomplete=off>
<button>Ask</button></form><div id=out></div>{_footer()}</div>
<script>
const out=document.getElementById('out'), esc=s=>String(s??'').replace(/[&<]/g,c=>c=='&'?'&amp;':'&lt;');
document.getElementById('f').onsubmit=async e=>{{e.preventDefault();const q=document.getElementById('q').value.trim();if(!q)return;
const card=document.createElement('div');card.className='card';card.innerHTML='<b>'+esc(q)+'</b><p class=meta>Working…</p>';out.prepend(card);
const r=await fetch('api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:q}})}});const d=await r.json();
const steps=d.steps||[];const rows=steps.filter(s=>s.kind=='tool').map(s=>'<tr><td><code>'+esc(s.name)+'</code></td><td><code>'+esc(String(s.arguments||'').slice(0,140))+'</code></td><td>'+(s.ok?'ok':'error')+'</td><td>'+(s.rows??'')+'</td></tr>').join('');
card.innerHTML='<b>'+esc(q)+'</b><div class=ans>'+esc(d.answer||d.detail)+'</div><p class=meta>'+(d.tools_called||[]).length+' tool calls · '+(d.model_calls??'')+' model calls · '+Math.round(d.latency_ms||0)+' ms · cost '+(d.cost_usd!=null?'$'+Number(d.cost_usd).toFixed(5):'unknown')+(d.ungrounded_numbers&&d.ungrounded_numbers.length?' · ungrounded: '+esc(d.ungrounded_numbers.join(', ')):'')+' · '+esc(d.status)+'</p>'+(rows?'<details><summary class=meta>Agent trace</summary><table><tr><th>Tool</th><th>Arguments</th><th>Result</th><th>Rows</th></tr>'+rows+'</table></details>':'');}};
</script>"""


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "as_of_date": _db().as_of_date(), "synthetic_data": using_fixture()}


@app.post("/api/ask")
async def api_ask(q: Question, request: Request) -> dict:
    if not q.question.strip():
        raise HTTPException(400, "Empty question")
    refusal = LIMITER.check(_client_ip(request))
    if refusal:
        raise HTTPException(429, refusal)
    res = await run_in_threadpool(_agent().ask, q.question)
    return res.to_dict()


def _latest_run() -> str | None:
    runs = sorted(glob.glob(os.path.join(RUNS, "*", "findings.json")))
    return os.path.dirname(runs[-1]) if runs else None


@app.get("/review", response_class=HTMLResponse)
def review_page() -> str:
    # The reviewer token is typed into a password field and sent as a header; it never goes in
    # a URL (URLs end up in browser history, proxy logs and screenshots). The server checks it.
    run = _latest_run()
    if not run:
        return (f"<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
                f"<title>Price-Watch review</title>{STYLE}<div class=wrap>{NAV}<h1>Price-Watch review</h1>"
                "<p class=sub>No Price-Watch run has been published to this deployment yet. Price-Watch runs from the "
                "command line (<code>dpa-pricewatch run</code>): SQL finds unusual price moves, one agent per move "
                "looks for the cause, code re-checks every number, and a reviewer approves findings here.</p>"
                f"<p>The first run on real data (6 signals, 6/6 verified by code, $0.0146) is documented with "
                f"screenshots in <a href=\"{REPO_URL}#step-6-price-watch-detect-in-sql-explain-with-agents-verify-in-code\">"
                f"README Step 6</a>.</p>{_footer()}</div>")
    d = json.load(open(os.path.join(run, "findings.json")))
    e = html.escape
    cards = []
    for f in d["findings"]:
        r = f["report"] or {}
        ev = "".join(f"<li><code>{e(str(x.get('tool')))}</code> {e(str(x.get('finding')))}</li>" for x in r.get("evidence", []))
        buttons = ("" if not f["verified"] or f["status"] != "pending_review" else
                   f"<p class=rev hidden><button onclick=\"decide({f['id']},'approve')\">Approve</button> "
                   f"<button class=ghost onclick=\"decide({f['id']},'reject')\">Reject</button></p>")
        cards.append(f"""<div class=card><b>[{f['id']}] {e(str(r.get('headline') or f['signal']['ndc_description']))}</b>
<p class=meta><span class="pill{'' if f['verified'] else ' bad'}">{'verified against data' if f['verified'] else 'failed verification'}</span>
 status <b>{e(f['status'])}</b> · cause {e(str(r.get('likely_cause')))} · confidence {e(str(r.get('confidence')))}
 · {f['tool_calls']} tool calls · {f['attempts']} attempt(s)</p><p>{e(str(r.get('explanation', '')))}</p><ul>{ev}</ul>
{''.join(f'<p class=meta>Verifier: {e(i)}</p>' for i in f['issues'])}{buttons}</div>""")
    m = d["meta"]
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Price-Watch review</title>{STYLE}<div class=wrap>{NAV}<h1>Price-Watch review</h1>
<p class=sub>NADAC as of {e(str(m.get('as_of_date')))} · {m['signals']} signals · {m['verified']} verified by code ·
 model {e(str(m['model']))} · cost {('$' + str(m.get('cost_usd'))) if m.get('cost_usd') is not None else 'unknown'}.
 Read-only for visitors; a reviewer enters their token to approve or reject findings for the brief.</p>
<form id=rf style="max-width:420px"><input id=tok type=password autocomplete=off placeholder="Reviewer token"><button class=ghost>Review</button></form>
<p id=rmsg class=meta></p>{''.join(cards)}{_footer()}</div>
<script>let TOKEN='';document.getElementById('rf').onsubmit=e=>{{e.preventDefault();TOKEN=document.getElementById('tok').value;
document.querySelectorAll('.rev').forEach(x=>x.hidden=!TOKEN);}};
async function decide(id,action){{const r=await fetch('api/review',{{method:'POST',headers:{{'Content-Type':'application/json','X-Admin-Token':TOKEN}},
body:JSON.stringify({{run:{json.dumps(os.path.basename(run))},id,action}})}});
if(r.ok){{location.reload();}}else{{document.getElementById('rmsg').textContent=r.status==403?'Wrong reviewer token.':'Could not save the decision.';}}}}</script>"""


@app.post("/api/review")
def api_review(dcs: Decision, x_admin_token: str | None = Header(default=None)) -> dict:
    if not _is_admin(x_admin_token):
        raise HTTPException(403, "Reviewer token required")
    run = os.path.join(RUNS, os.path.basename(dcs.run))
    if not os.path.exists(os.path.join(run, "findings.json")):
        raise HTTPException(404, "Run not found")
    try:
        pricewatch.review(run, [dcs.id] if dcs.action == "approve" else [], [dcs.id] if dcs.action == "reject" else [])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    pricewatch.publish(run)
    return {"ok": True}


@app.get("/brief", response_class=HTMLResponse)
def brief() -> str:
    run = _latest_run()
    if not run:
        raise HTTPException(404, "No runs yet")
    pricewatch.publish(run)
    return open(os.path.join(run, "brief.html")).read().replace("<h1>", NAV.replace("<nav>", '<nav style="display:flex;gap:14px;font-weight:600">') + "<h1>", 1)


# Last, so the UI and API routes above match first: MCP streamable HTTP at /mcp.
app.mount("/", _mcp()[0])

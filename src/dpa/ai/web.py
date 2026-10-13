"""Small web app: chat with the Ask agent (dpa.agent) and review Price-Watch findings.

    DPA_GOLD_DIR=... DPA_MODEL=... uvicorn dpa.ai.web:app --port 8080
"""
from __future__ import annotations

import functools
import glob
import html
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from dpa.agent.data import DEFAULT_FIXTURE
from dpa.agent.factory import DEFAULT_MODEL, make_agent
from dpa.ai import pricewatch

app = FastAPI(title="Drug Pricing Intelligence Agent")
RUNS = os.getenv("DPA_PRICEWATCH_DIR", "pricewatch_runs")

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
    return make_agent(mode="live", model=os.environ.get("DPA_MODEL", DEFAULT_MODEL),
                      gold_dir=os.environ.get("DPA_GOLD_DIR", DEFAULT_FIXTURE))


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
<button>Ask</button></form><div id=out></div></div>
<script>
const out=document.getElementById('out'), esc=s=>String(s??'').replace(/[&<]/g,c=>c=='&'?'&amp;':'&lt;');
document.getElementById('f').onsubmit=async e=>{{e.preventDefault();const q=document.getElementById('q').value.trim();if(!q)return;
const card=document.createElement('div');card.className='card';card.innerHTML='<b>'+esc(q)+'</b><p class=meta>Working…</p>';out.prepend(card);
const r=await fetch('api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:q}})}});const d=await r.json();
const steps=d.steps||[];const rows=steps.filter(s=>s.kind=='tool').map(s=>'<tr><td><code>'+esc(s.name)+'</code></td><td><code>'+esc(String(s.arguments||'').slice(0,140))+'</code></td><td>'+(s.ok?'ok':'error')+'</td><td>'+(s.rows??'')+'</td></tr>').join('');
card.innerHTML='<b>'+esc(q)+'</b><div class=ans>'+esc(d.answer||d.detail)+'</div><p class=meta>'+(d.tools_called||[]).length+' tool calls · '+(d.model_calls??'')+' model calls · '+Math.round(d.latency_ms||0)+' ms · cost '+(d.cost_usd!=null?'$'+Number(d.cost_usd).toFixed(5):'unknown')+(d.ungrounded_numbers&&d.ungrounded_numbers.length?' · ungrounded: '+esc(d.ungrounded_numbers.join(', ')):'')+' · '+esc(d.status)+'</p>'+(rows?'<details><summary class=meta>Agent trace</summary><table><tr><th>Tool</th><th>Arguments</th><th>Result</th><th>Rows</th></tr>'+rows+'</table></details>':'');}};
</script>"""


@app.post("/api/ask")
async def api_ask(q: Question) -> dict:
    if not q.question.strip():
        raise HTTPException(400, "Empty question")
    res = await run_in_threadpool(_agent().ask, q.question)
    return res.to_dict()


def _latest_run() -> str | None:
    runs = sorted(glob.glob(os.path.join(RUNS, "*", "findings.json")))
    return os.path.dirname(runs[-1]) if runs else None


@app.get("/review", response_class=HTMLResponse)
def review_page() -> str:
    run = _latest_run()
    if not run:
        return f"<!doctype html><meta charset=utf-8>{STYLE}<div class=wrap>{NAV}<h1>No Price-Watch runs yet</h1></div>"
    d = json.load(open(os.path.join(run, "findings.json")))
    e = html.escape
    cards = []
    for f in d["findings"]:
        r = f["report"] or {}
        ev = "".join(f"<li><code>{e(str(x.get('tool')))}</code> {e(str(x.get('finding')))}</li>" for x in r.get("evidence", []))
        buttons = ("" if not f["verified"] or f["status"] != "pending_review" else
                   f"<p><button onclick=\"decide({f['id']},'approve')\">Approve</button> "
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
 Approve findings to include them in the brief.</p>{''.join(cards)}</div>
<script>async function decide(id,action){{await fetch('api/review',{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{run:{json.dumps(os.path.basename(run))},id,action}})}});location.reload();}}</script>"""


@app.post("/api/review")
def api_review(dcs: Decision) -> dict:
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

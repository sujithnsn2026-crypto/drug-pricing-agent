"""Run the golden set through the agent and write a run folder.

    evals/runs/<run_id>/
        results.jsonl    one line per question: answer, status, steps, tokens, cost, score
        summary.json     aggregate metrics + run metadata (model, prompt ref, mode, git sha)
        report.md        human-readable report (also used as the GitHub Actions job summary)
        traces.jsonl     OpenTelemetry spans for every question
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from dpa.agent.agent import AskAgent
from dpa.evals.report import write_report
from dpa.evals.scoring import GoldenItem, load_golden, score_item, summarize


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


def run_eval(agent: AskAgent, out_dir: str, mode: str, items: list[GoldenItem] | None = None,
             concurrency: int = 4) -> dict:
    items = items or load_golden()
    by_id = {i.id: i for i in items}
    os.makedirs(out_dir, exist_ok=True)

    def one(item: GoldenItem) -> dict:
        r = agent.ask(item.question)
        row = {"id": item.id, **r.to_dict()}
        row["score"] = score_item(item, r.answer, r.status, r.tools_called).__dict__
        return row

    workers = 1 if mode == "replay" else max(1, concurrency)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(one, items))

    with open(os.path.join(out_dir, "results.jsonl"), "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    summary = {
        "run": {
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "mode": mode, "model": agent.model, "prompt": agent.prompt.ref,
            "git_sha": git_sha(), "golden_items": len(items),
        },
        "metrics": summarize(rows, by_id),
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    write_report(out_dir, summary, rows, by_id)
    return summary

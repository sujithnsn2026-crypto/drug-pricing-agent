"""Markdown reports for eval runs, gate results and A/B comparisons."""
from __future__ import annotations

import os

METRIC_LABELS = [
    ("accuracy", "Answer accuracy", "pct"),
    ("grounding_rate", "Numbers grounded in tool results", "pct"),
    ("tool_selection_accuracy", "Right tool chosen", "pct"),
    ("refusal_recall", "Out-of-scope refused", "pct"),
    ("false_refusal_rate", "In-scope wrongly refused", "pct"),
    ("injection_block_rate", "Direct injections blocked by guard", "pct"),
    ("grounding_retry_rate", "Needed a grounding retry", "pct"),
    ("run_sql_rate", "Fell back to run_sql", "pct"),
    ("tool_error_rate", "Tool calls that errored", "pct"),
    ("errors", "Errors / step-limit hits", "int"),
    ("p50_latency_ms", "Latency p50 (ms)", "num"),
    ("p95_latency_ms", "Latency p95 (ms)", "num"),
    ("avg_model_calls", "Model calls per question", "num"),
    ("avg_input_tokens", "Input tokens per question", "num"),
    ("avg_output_tokens", "Output tokens per question", "num"),
    ("avg_cost_usd", "Cost per question (USD)", "usd"),
    ("total_cost_usd", "Cost of this run (USD)", "usd"),
]


def fmt(v, kind: str) -> str:
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "usd":
        return f"${v:.5f}"
    if kind == "int":
        return str(int(v))
    return f"{v:,}"


def write_report(out_dir: str, summary: dict, rows: list[dict], items: dict) -> str:
    m, run = summary["metrics"], summary["run"]
    lines = [
        "# Ask agent eval report", "",
        f"Model `{run['model']}` · prompt `{run['prompt']}` · mode `{run['mode']}` · "
        f"commit `{run['git_sha']}` · {run['created_utc']}", "",
        "| Metric | Value |", "|---|---|",
        *[f"| {label} | {fmt(m.get(k), kind)} |" for k, label, kind in METRIC_LABELS],
        "", "## By category", "", "| Category | Questions | Accuracy |", "|---|---|---|",
        *[f"| {c} | {v['n']} | {fmt(v['accuracy'], 'pct')} |" for c, v in m["by_category"].items()],
        "", "## Failures", "",
    ]
    failed = [r for r in rows if not r["score"]["correct"]]
    if not failed:
        lines.append("None.")
    for r in failed:
        ans = (r["answer"] or "").replace("\n", " ")
        lines += [f"**{r['id']}** ({r['score']['category']}): {items[r['id']].question}",
                  f"- why: {'; '.join(r['score']['failures'])}",
                  f"- tools: {', '.join(r['tools_called']) or 'none'}",
                  f"- answer: {ans[:400]}", ""]
    ungrounded = [r for r in rows if r["ungrounded_numbers"]]
    if ungrounded:
        lines += ["", "## Ungrounded numbers (after retry)", ""]
        lines += [f"- {r['id']}: {', '.join(r['ungrounded_numbers'])}" for r in ungrounded]
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write(text)
    return text


def comparison_markdown(a: dict, b: dict) -> str:
    ma, mb = a["metrics"], b["metrics"]
    lines = ["# Eval comparison", "",
             f"A: `{a['run']['model']}` · `{a['run']['prompt']}`  ",
             f"B: `{b['run']['model']}` · `{b['run']['prompt']}`", "",
             "| Metric | A | B | B - A |", "|---|---|---|---|"]
    for k, label, kind in METRIC_LABELS:
        va, vb = ma.get(k), mb.get(k)
        if va is None or vb is None:
            delta = "n/a"
        elif kind == "pct":
            delta = f"{(vb - va) * 100:+.1f} pts"
        elif kind == "usd":
            delta = f"{(vb - va) / va * 100:+.0f}%" if va else "n/a"
        else:
            delta = f"{vb - va:+,.1f}"
        lines.append(f"| {label} | {fmt(va, kind)} | {fmt(vb, kind)} | {delta} |")
    fa, fb = set(ma["failed_ids"]), set(mb["failed_ids"])
    lines += ["", f"Fixed in B: {', '.join(sorted(fa - fb)) or 'none'}  ",
              f"Broken in B: {', '.join(sorted(fb - fa)) or 'none'}"]
    return "\n".join(lines) + "\n"

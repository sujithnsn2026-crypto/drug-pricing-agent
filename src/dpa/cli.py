"""Command line entry points.

  dpa-ask "Which generic rose most in 90 days?"            ask once (live Azure OpenAI)
  dpa-eval run    --mode replay                             golden set from the cassette (free, CI)
  dpa-eval run    --mode record --model gpt-4.1-mini        golden set live, saving a new cassette
  dpa-eval gate   evals/runs/<run>                          pass/fail vs thresholds + baseline
  dpa-eval compare evals/runs/<a> evals/runs/<b>            A/B two runs (prompts or models)
  dpa-eval promote evals/runs/<run>                         make a run the new baseline
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys

from dpa.agent.data import DEFAULT_FIXTURE
from dpa.agent.factory import DEFAULT_MODEL, MODES, ROOT, make_agent


def _load_dotenv() -> None:
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ask_main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(prog="dpa-ask", description="Ask the drug pricing agent one question.")
    p.add_argument("question")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Azure OpenAI deployment name")
    p.add_argument("--prompt-version", default=None, help="default: the active version in prompts/registry.yaml")
    p.add_argument("--mode", choices=MODES, default="live")
    p.add_argument("--gold-dir", default=DEFAULT_FIXTURE)
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = p.parse_args(argv)

    from dpa.llmops.tracing import setup_tracing, shutdown_tracing

    setup_tracing(jsonl_path=os.path.join(ROOT, "_traces", "ask.jsonl"))
    agent = make_agent(a.mode, a.model, a.prompt_version, a.gold_dir)
    r = agent.ask(a.question)
    shutdown_tracing()
    if a.json:
        print(json.dumps(r.to_dict(), indent=2, default=str))
        return 0
    print(r.answer)
    cost = "n/a" if r.cost_usd is None else f"${r.cost_usd:.5f}"
    print(f"\n[{r.status} · {r.model} · {r.prompt_ref} · tools: {', '.join(r.tools_called) or 'none'} · "
          f"{r.usage.input_tokens}+{r.usage.output_tokens} tokens · {cost} · {r.latency_ms:.0f} ms · "
          f"trace {r.trace_id}]")
    if r.ungrounded_numbers:
        print(f"[warning: numbers not found in tool results: {', '.join(r.ungrounded_numbers)}]")
    return 0 if r.status in ("answered", "blocked_input") else 1


def eval_main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(prog="dpa-eval", description="Evaluate the Ask agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the golden set")
    r.add_argument("--mode", choices=MODES, default="replay")
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--prompt-version", default=None)
    r.add_argument("--cassette", default=None, help="default: evals/cassettes/<model>__ask_agent-<version>.jsonl")
    r.add_argument("--gold-dir", default=DEFAULT_FIXTURE)
    r.add_argument("--out", default=None, help="default: evals/runs/<timestamp>_<model>_<prompt>")
    r.add_argument("--only", default=None, help="comma-separated golden ids")
    r.add_argument("--concurrency", type=int, default=4)

    g = sub.add_parser("gate", help="pass/fail a run")
    g.add_argument("run_dir")
    g.add_argument("--baseline", default=os.path.join(ROOT, "evals", "baseline"))
    g.add_argument("--no-baseline", action="store_true")
    g.add_argument("--summary-file", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                   help="append the markdown report here (GitHub Actions job summary)")

    c = sub.add_parser("compare", help="compare two runs")
    c.add_argument("run_a")
    c.add_argument("run_b")

    pr = sub.add_parser("promote", help="make a run the new baseline")
    pr.add_argument("run_dir")

    a = p.parse_args(argv)

    if a.cmd == "run":
        from dpa.evals.runner import run_eval
        from dpa.evals.scoring import load_golden
        from dpa.llmops.tracing import setup_tracing, shutdown_tracing
        from dpa.llmops.prompts import load_prompt

        version = load_prompt("ask_agent", a.prompt_version).version
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = a.out or os.path.join(ROOT, "evals", "runs", f"{stamp}_{a.model}_{version}_{a.mode}")
        items = load_golden()
        if a.only:
            keep = set(a.only.split(","))
            items = [i for i in items if i.id in keep]
        setup_tracing(jsonl_path=os.path.join(out, "traces.jsonl"))
        agent = make_agent(a.mode, a.model, version, a.gold_dir, a.cassette)
        summary = run_eval(agent, out, a.mode, items, a.concurrency)
        shutdown_tracing()
        with open(os.path.join(out, "report.md")) as f:
            print(f.read())
        print(f"run folder: {os.path.relpath(out, os.getcwd())}")
        return 0 if summary["metrics"]["errors"] == 0 else 1

    if a.cmd == "gate":
        from dpa.evals.gate import evaluate_gate

        res = evaluate_gate(a.run_dir, None if a.no_baseline else a.baseline)
        md = res.markdown()
        print(md)
        if a.summary_file:
            with open(os.path.join(a.run_dir, "report.md")) as f:
                report = f.read()
            with open(a.summary_file, "a") as f:
                f.write(md + "\n" + report)
        return 0 if res.passed else 1

    if a.cmd == "compare":
        from dpa.evals.gate import load_summary
        from dpa.evals.report import comparison_markdown

        print(comparison_markdown(load_summary(a.run_a), load_summary(a.run_b)))
        return 0

    if a.cmd == "promote":
        dst = os.path.join(ROOT, "evals", "baseline")
        os.makedirs(dst, exist_ok=True)
        for name in ("summary.json", "results.jsonl", "report.md"):
            shutil.copy(os.path.join(a.run_dir, name), os.path.join(dst, name))
        print(f"baseline updated from {a.run_dir}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(eval_main())

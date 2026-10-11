"""The eval gate: pass/fail a run against absolute thresholds, a committed
baseline (no regressions), and a list of critical questions that must pass
individually (safety cases can't be averaged away by easy wins elsewhere).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
GATE_FILE = os.path.join(ROOT, "evals", "gate.yaml")
BASELINE_DIR = os.path.join(ROOT, "evals", "baseline")


@dataclass
class GateResult:
    passed: bool = True
    lines: list[str] = field(default_factory=list)

    def add(self, ok: bool, text: str) -> None:
        self.lines.append(f"{'PASS' if ok else 'FAIL'}  {text}")
        self.passed = self.passed and ok

    def markdown(self) -> str:
        head = "## Eval gate: " + ("PASSED" if self.passed else "FAILED")
        body = "\n".join(f"- {'✅' if line.startswith('PASS') else '❌'} {line[6:]}" for line in self.lines)
        return f"{head}\n\n{body}\n"


def load_summary(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "summary.json")) as f:
        return json.load(f)


def load_results(run_dir: str) -> dict[str, dict]:
    with open(os.path.join(run_dir, "results.jsonl")) as f:
        return {r["id"]: r for r in map(json.loads, f)}


def evaluate_gate(run_dir: str, baseline_dir: str | None = BASELINE_DIR, gate_file: str = GATE_FILE) -> GateResult:
    with open(gate_file) as f:
        cfg = yaml.safe_load(f)
    m = load_summary(run_dir)["metrics"]
    res = GateResult()

    for metric, rule in (cfg.get("thresholds") or {}).items():
        v = m.get(metric)
        if v is None:
            res.add(not rule.get("required", True), f"{metric}: no value")
            continue
        if "min" in rule:
            res.add(v >= rule["min"], f"{metric} = {v} (min {rule['min']})")
        if "max" in rule:
            res.add(v <= rule["max"], f"{metric} = {v} (max {rule['max']})")

    results = load_results(run_dir)
    for qid in cfg.get("critical_items") or []:
        r = results.get(qid)
        ok = r is not None and r["score"]["correct"]
        res.add(ok, f"critical question {qid}" + ("" if ok else f": {'; '.join(r['score']['failures']) if r else 'missing'}"))

    if baseline_dir and os.path.exists(os.path.join(baseline_dir, "summary.json")):
        b = load_summary(baseline_dir)["metrics"]
        for metric, rule in (cfg.get("regression") or {}).items():
            v, bv = m.get(metric), b.get(metric)
            if v is None or bv is None:
                continue
            if "max_drop" in rule:
                res.add(bv - v <= rule["max_drop"] + 1e-9,
                        f"{metric} vs baseline: {bv} -> {v} (max drop {rule['max_drop']})")
            if "max_increase_pct" in rule and bv:
                inc = (v - bv) / bv * 100
                res.add(inc <= rule["max_increase_pct"],
                        f"{metric} vs baseline: {bv} -> {v} ({inc:+.0f}%, max +{rule['max_increase_pct']}%)")
    else:
        res.lines.append("PASS  no baseline yet (first run): thresholds only")
    return res

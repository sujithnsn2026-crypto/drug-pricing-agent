"""Score one agent answer against its golden expectations, and aggregate a run.

All checks are deterministic code (substring, number and tool checks), so a
score never depends on another model's mood. Free-text quality that code can't
judge is out of scope for the gate on purpose.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

import yaml

from dpa.llmops.guardrails import _numbers

GOLDEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "evals", "golden.yaml")
WORD_NUMBERS = {"once": 1, "one": 1, "twice": 2, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


@dataclass
class GoldenItem:
    id: str
    category: str
    question: str
    expect: dict
    source_sql: str | None = None
    note: str = ""


def load_golden(path: str = GOLDEN_FILE) -> list[GoldenItem]:
    with open(path) as f:
        doc = yaml.safe_load(f)
    items = [GoldenItem(**it) for it in doc["items"]]
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate golden ids")
    return items


def is_refusal(answer: str, status: str) -> bool:
    return status == "blocked_input" or (answer or "").strip().lower().startswith("out of scope")


def answer_numbers(answer: str, words: bool = False) -> list[tuple[float, int]]:
    nums = [(v, d) for _, v, d in _numbers(answer or "") if d >= 0]
    if words:
        low = (answer or "").lower()
        for w, n in WORD_NUMBERS.items():
            if re.search(rf"\b{w}\b", low):
                nums.append((float(n), 0))
    return nums


def number_present(expected: float, nums: list[tuple[float, int]]) -> bool:
    for val, dec in nums:
        tol = max(0.005, 0.5 * 10 ** (-dec)) + 1e-9
        if abs(abs(val) - abs(expected)) <= tol:
            return True
    return False


@dataclass
class ItemScore:
    id: str
    category: str
    correct: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    refused: bool = False
    tool_ok: bool | None = None


def score_item(item: GoldenItem, answer: str, status: str, tools_called: list[str]) -> ItemScore:
    e = item.expect
    low = (answer or "").lower()
    refused = is_refusal(answer, status)
    s = ItemScore(item.id, item.category, True, refused=refused)

    def check(name: str, ok: bool, why: str = "") -> None:
        s.checks[name] = ok
        if not ok:
            s.correct = False
            s.failures.append(why or name)

    if status in ("error", "max_steps"):
        check("status", False, f"status={status}")
    want_refusal = bool(e.get("refuse", False))
    if ("refuse" in e or refused) and not e.get("refuse_ok"):
        check("refusal", refused == want_refusal,
              "should refuse" if want_refusal else "refused an in-scope question")
    if e.get("blocked"):
        check("blocked", status == "blocked_input", "input guardrail did not block")
    for sub in e.get("contains", []):
        check(f"contains:{sub}", sub.lower() in low, f"missing '{sub}'")
    for group in e.get("any_of", []):
        check(f"any_of:{group[0]}", any(g.lower() in low for g in group), f"none of {group}")
    for sub in e.get("not_contains", []):
        check(f"not_contains:{sub}", sub.lower() not in low, f"contains forbidden '{sub}'")
    if e.get("numbers"):
        nums = answer_numbers(answer, words=bool(e.get("numbers_words")))
        for n in e["numbers"]:
            check(f"number:{n}", number_present(float(n), nums), f"missing number {n}")
    if e.get("tools_any"):
        s.tool_ok = any(t in tools_called for t in e["tools_any"])
        s.checks["tools"] = s.tool_ok  # tracked separately, not part of correctness
    return s


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def _rate(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def summarize(rows: list[dict], items: dict[str, GoldenItem]) -> dict:
    """rows: one dict per question with keys from AgentResult.to_dict() plus 'score'."""
    n = len(rows)
    correct = sum(r["score"]["correct"] for r in rows)
    cats: dict[str, list[bool]] = {}
    for r in rows:
        cats.setdefault(r["score"]["category"], []).append(r["score"]["correct"])

    answered = [r for r in rows if r["status"] == "answered" and not r["score"]["refused"]]
    grounded = [r for r in answered if not r["ungrounded_numbers"]]
    tool_rows = [r for r in rows if r["score"]["tool_ok"] is not None]
    want_ref = [r for r in rows if items[r["id"]].expect.get("refuse")]
    in_scope = [r for r in rows if not items[r["id"]].expect.get("refuse")
                and not items[r["id"]].expect.get("refuse_ok")]
    want_block = [r for r in rows if items[r["id"]].expect.get("blocked")]
    lat = [sum(s["latency_ms"] for s in r["steps"]) for r in rows if r["status"] != "blocked_input"]
    costs = [r["cost_usd"] for r in rows]
    known_costs = [c for c in costs if c is not None]

    return {
        "n": n,
        "accuracy": _rate(correct, n),
        "by_category": {c: {"n": len(v), "accuracy": _rate(sum(v), len(v))} for c, v in sorted(cats.items())},
        "grounding_rate": _rate(len(grounded), len(answered)),
        "grounding_retry_rate": _rate(sum(1 for r in rows if r["grounding_retries"]), n),
        "tool_selection_accuracy": _rate(sum(r["score"]["tool_ok"] for r in tool_rows), len(tool_rows)),
        "refusal_recall": _rate(sum(r["score"]["refused"] for r in want_ref), len(want_ref)),
        "false_refusal_rate": _rate(sum(r["score"]["refused"] for r in in_scope), len(in_scope)),
        "injection_block_rate": _rate(sum(r["status"] == "blocked_input" for r in want_block), len(want_block)),
        "run_sql_rate": _rate(sum("run_sql" in r["tools_called"] for r in answered), len(answered)),
        "tool_error_rate": _rate(
            sum(1 for r in rows for s in r["steps"] if s["kind"] == "tool" and not s["ok"]),
            max(1, sum(1 for r in rows for s in r["steps"] if s["kind"] == "tool"))),
        "errors": sum(r["status"] in ("error", "max_steps") for r in rows),
        "p50_latency_ms": _pct(lat, 0.5),
        "p95_latency_ms": _pct(lat, 0.95),
        "avg_model_calls": round(sum(r["model_calls"] for r in rows) / n, 2) if n else None,
        "avg_input_tokens": round(sum(r["usage"]["input_tokens"] for r in rows) / n) if n else None,
        "avg_output_tokens": round(sum(r["usage"]["output_tokens"] for r in rows) / n) if n else None,
        "avg_cost_usd": round(sum(known_costs) / n, 6) if n and len(known_costs) == n else None,
        "total_cost_usd": round(sum(known_costs), 6) if len(known_costs) == n else None,
        "failed_ids": [r["id"] for r in rows if not r["score"]["correct"]],
    }

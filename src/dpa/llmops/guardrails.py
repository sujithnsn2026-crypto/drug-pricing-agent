"""Guardrails, all deterministic code (no extra model calls):

1. Input check     blocks prompt-injection attempts and oversized questions before
                   any tokens are spent.
2. Tool-output     scrubs instruction-like text out of tool results. FDA free-text
   sanitiser       fields (e.g. shortage reasons) are untrusted input; a planted
                   instruction there must never reach the model as an instruction.
3. Number          every number in the final answer must be traceable to a tool
   grounding       result (or the question). Ungrounded numbers trigger one
                   self-correction round, then a visible flag.

The patterns are deliberately narrow: a false block on a real pricing question
costs more trust than a miss, and the model is a second line of defence (the
system prompt) with the eval suite measuring both rates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_QUESTION_CHARS = 2000

INJECTION_PATTERNS = [
    r"\bignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?|directions)",
    r"\bdisregard\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your)\s+(instructions|rules|prompts?)",
    r"\bforget\s+(all\s+|everything\s+|your\s+)?(previous\s+|prior\s+)?(instructions|rules)",
    r"\b(reveal|show|print|repeat|output|tell me)\s+(me\s+)?(your|the)\s+(system\s+prompt|instructions|hidden\s+prompt)",
    r"\bwhat\s+(is|are)\s+your\s+(system\s+prompt|instructions)",
    r"\byou\s+are\s+now\s+(a|an|in)\b",
    r"\b(developer|god|dan|jailbreak)\s+mode\b",
    r"\bnew\s+instructions\s*:",
    r"\bsystem\s*:\s*you\s+(must|are|will)",
    r"<\s*/?\s*(system|instructions?)\s*>",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE)

BLOCKED_REPLY = ("Out of scope: I can only answer questions about US drug acquisition prices (CMS NADAC) "
                 "and FDA drug shortages, and I can't change or reveal my instructions.")


@dataclass
class InputCheck:
    blocked: bool
    reason: str = ""
    matched: str = ""


def check_input(question: str) -> InputCheck:
    if not question or not question.strip():
        return InputCheck(True, "empty")
    if len(question) > MAX_QUESTION_CHARS:
        return InputCheck(True, "too_long")
    m = _INJECTION_RE.search(question)
    if m:
        return InputCheck(True, "prompt_injection", m.group(0))
    return InputCheck(False)


def sanitize_tool_output(text: str) -> tuple[str, int]:
    """Replace instruction-like spans inside a tool result. Returns (clean text, spans removed).

    Works on the JSON string field by field so the result stays valid JSON.
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return text, 0
    removed = 0

    def clean(v):
        nonlocal removed
        if isinstance(v, str) and _INJECTION_RE.search(v):
            removed += 1
            # Keep the factual part before the injected instruction, drop the rest.
            head = _INJECTION_RE.split(v, maxsplit=1)[0].rstrip(" .;:-")
            return (head + " " if head else "") + "[removed: text resembling instructions]"
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        return v

    doc = clean(doc)
    return (json.dumps(doc, default=str) if removed else text), removed


# --------------------------------------------------------------------------- #
# Number grounding
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\w])")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|"
                      r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b|"
                      r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{4}\b",
                      re.IGNORECASE)
_NDC_RE = re.compile(r"\b\d{4,5}-\d{3,4}-\d{1,2}\b")
# Small counts, list numbering, window lengths and years are not claims to verify.
CONTEXT_NUMBERS = {30.0, 60.0, 90.0, 180.0, 365.0, 100.0}


def _numbers(text: str) -> list[tuple[str, float, int]]:
    """(token, value, decimals) for each number in text, skipping dates and NDC strings."""
    text = _DATE_RE.sub(" ", text)
    text = _NDC_RE.sub(" ", text)
    out = []
    for m in _NUM_RE.finditer(text):
        whole, frac = m.group(1), m.group(2) or ""
        token = whole + frac
        digits = whole.replace(",", "")
        if len(digits) >= 9 and not frac:  # an NDC or other identifier
            out.append((digits, float("nan"), -1))
            continue
        out.append((token, float(digits + frac), len(frac) - 1 if frac else 0))
    return out


@dataclass
class GroundingResult:
    checked: int = 0
    ungrounded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded

    @property
    def rate(self) -> float:
        return 1.0 if self.checked == 0 else 1 - len(self.ungrounded) / self.checked


def check_grounding(answer: str, evidence: list[str], question: str = "") -> GroundingResult:
    source_text = " ".join(evidence) + " " + question
    values = [v for _, v, d in _numbers(source_text) if d >= 0]
    ids = {tok for tok, _, d in _numbers(source_text) if d < 0}
    ids |= {re.sub(r"\D", "", m.group(0)) for m in _NDC_RE.finditer(source_text)}

    res = GroundingResult()
    for tok, val, dec in _numbers(answer or ""):
        if dec < 0:  # identifier: must appear verbatim in the evidence
            res.checked += 1
            if tok not in ids:
                res.ungrounded.append(tok)
            continue
        if (dec == 0 and val <= 12) or val in CONTEXT_NUMBERS or (dec == 0 and 1990 <= val <= 2100):
            continue
        res.checked += 1
        tol = 0.5 * 10 ** (-dec) + 1e-9
        if not any(abs(val - abs(t)) <= tol for t in values):
            res.ungrounded.append(tok)
    return res

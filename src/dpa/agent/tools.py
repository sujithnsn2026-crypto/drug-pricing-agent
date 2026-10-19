"""The agent's tools. All read-only, all parameterised, all return JSON.

Four purpose-built tools cover the common questions with fixed, tested SQL;
`run_sql` is the escape hatch for anything else and goes through the SQL guard.
Preferring fixed tools keeps answers cheap, exact and repeatable; the eval
report tracks how often the model falls back to run_sql.

Tool results are data. They are wrapped as {"ok": ..., "rows": [...]} and the
system prompt tells the model to treat text inside them as data, never as
instructions (FDA text fields are untrusted input).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from dpa.agent.data import DuckDbGold
from dpa.agent.sql_guard import SqlRejected, guard_sql

MAX_ROWS_TO_MODEL = 25

CURRENT_COLS = (
    "ndc11, ndc_description, generic_name, brand_name, labeler_name, is_generic, "
    "current_price, pricing_unit, price_effective_date, price_90d_ago, pct_change_90d, "
    "price_1y_ago, pct_change_1y, in_shortage, shortage_status, shortage_reason, "
    "shortage_posted_date, as_of_date"
)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        """OpenAI / Azure OpenAI function-calling schema."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


class ToolError(Exception):
    pass


def _fold(word: str) -> str:
    """Fold simple English plurals so "tablets" matches "TABLET" and "capsules" matches "CAPSULE"."""
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss") and not word[-2].isdigit():
        return word[:-1]
    return word


def _clamp_limit(limit: Any, default: int = 10) -> int:
    try:
        return max(1, min(int(limit), MAX_ROWS_TO_MODEL))
    except (TypeError, ValueError):
        return default


def build_tools(db: DuckDbGold) -> dict[str, Tool]:
    def search_drugs(query: str, limit: int = 10) -> list[dict]:
        """Every word of the query must appear somewhere in the description, generic or brand name.

        Matching the query as one phrase failed on real CMS text: "verapamil 120 mg tablets"
        is "VERAPAMIL ER 120 MG TABLET" in NADAC (extra "ER", singular "TABLET"). Words are
        matched one by one, plurals are folded, and if nothing matches all words, the first
        word (normally the drug name) is tried alone so the model sees the close candidates.
        """
        q = (query or "").strip()
        if len(q) < 2:
            raise ToolError("query must be at least 2 characters")
        words = [_fold(w) for w in re.findall(r"[a-z0-9.%/-]+", q.lower())][:8]
        words = [w for w in words if w] or [q.lower()]
        text = "lower(concat_ws(' ', ndc_description, generic_name, brand_name))"
        base = (f"SELECT ndc11, ndc_description, generic_name, brand_name, is_generic, current_price, "
                f"pricing_unit, in_shortage FROM drug_price_current WHERE ")
        tail = f" ORDER BY ndc_description, ndc11 LIMIT {_clamp_limit(limit)}"
        rows = db.query(base + " AND ".join([f"{text} LIKE ?"] * len(words)) + tail, [f"%{w}%" for w in words])
        if not rows and len(words) > 1:
            rows = db.query(base + f"{text} LIKE ?" + tail, [f"%{words[0]}%"])
        return rows

    def get_drug_price(ndc11: str) -> list[dict]:
        digits = "".join(ch for ch in str(ndc11) if ch.isdigit())
        if len(digits) != 11:
            raise ToolError("ndc11 must be an 11-digit NDC (5-4-2 without hyphens). Use search_drugs first.")
        rows = db.query(f"SELECT {CURRENT_COLS} FROM drug_price_current WHERE ndc11 = ?", [digits])
        if not rows:
            raise ToolError(f"no current NADAC price for NDC {digits}")
        return rows

    def top_price_changes(window: str = "90d", direction: str = "increase", generic_only: bool | None = None,
                          in_shortage: bool | None = None, limit: int = 10) -> list[dict]:
        col = {"90d": "pct_change_90d", "1y": "pct_change_1y"}.get(window)
        if col is None:
            raise ToolError("window must be '90d' or '1y'")
        if direction not in ("increase", "decrease"):
            raise ToolError("direction must be 'increase' or 'decrease'")
        where = [f"{col} IS NOT NULL", f"{col} {'>' if direction == 'increase' else '<'} 0"]
        params: list = []
        if generic_only is not None:
            where.append("is_generic = ?")
            params.append(bool(generic_only))
        if in_shortage is not None:
            where.append("in_shortage = ?")
            params.append(bool(in_shortage))
        order = "DESC" if direction == "increase" else "ASC"
        return db.query(
            f"SELECT {CURRENT_COLS} FROM drug_price_current WHERE {' AND '.join(where)} "
            f"ORDER BY {col} {order}, ndc11 LIMIT {_clamp_limit(limit)}",
            params,
        )

    def price_history(ndc11: str) -> list[dict]:
        digits = "".join(ch for ch in str(ndc11) if ch.isdigit())
        if len(digits) != 11:
            raise ToolError("ndc11 must be an 11-digit NDC. Use search_drugs first.")
        rows = db.query(
            "SELECT ndc11, valid_from, valid_to, is_current, nadac_per_unit, pricing_unit, ndc_description "
            "FROM nadac_price_history WHERE ndc11 = ? ORDER BY valid_from",
            [digits],
        )
        if not rows:
            raise ToolError(f"no price history for NDC {digits}")
        return rows

    def run_sql(sql: str) -> list[dict]:
        g = guard_sql(sql)
        return db.query(g.sql)

    obj = lambda props, req: {"type": "object", "properties": props, "required": req,  # noqa: E731
                              "additionalProperties": False}
    tools = [
        Tool("search_drugs",
             "Find drugs by name (generic, brand or NADAC description, partial match). Returns NDCs and "
             "current price. Use this first when the user names a drug.",
             obj({"query": {"type": "string", "description": "drug name, e.g. 'amoxicillin'"},
                  "limit": {"type": "integer", "description": "max rows, 1-25"}}, ["query"]),
             search_drugs),
        Tool("get_drug_price",
             "Current NADAC price for one NDC, with 90-day and 1-year % change and FDA shortage status.",
             obj({"ndc11": {"type": "string", "description": "11-digit NDC"}}, ["ndc11"]),
             get_drug_price),
        Tool("top_price_changes",
             "Rank drugs by % price change over the last 90 days or 1 year. Optional filters: generic "
             "only, in a current FDA shortage. Use for 'biggest increase/decrease' questions.",
             obj({"window": {"type": "string", "enum": ["90d", "1y"]},
                  "direction": {"type": "string", "enum": ["increase", "decrease"]},
                  "generic_only": {"type": ["boolean", "null"], "description": "true=generics, false=brands"},
                  "in_shortage": {"type": ["boolean", "null"], "description": "true=only drugs in shortage"},
                  "limit": {"type": "integer", "description": "max rows, 1-25"}}, ["window", "direction"]),
             top_price_changes),
        Tool("price_history",
             "Full price history (SCD Type 2 versions) for one NDC: each price and the dates it was valid.",
             obj({"ndc11": {"type": "string", "description": "11-digit NDC"}}, ["ndc11"]),
             price_history),
        Tool("run_sql",
             "Run one read-only SELECT (DuckDB SQL) when no other tool fits, e.g. counts or averages. "
             "Tables: drug_price_current (one row per active NDC: ndc11, ndc_description, generic_name, "
             "brand_name, is_generic, current_price, pricing_unit, pct_change_90d, pct_change_1y, "
             "in_shortage, shortage_status, shortage_reason, as_of_date), fact_price_change (ndc11, "
             "change_date, old_price, new_price, pct_change, ndc_description, is_generic), "
             "nadac_price_history (ndc11, valid_from, valid_to, is_current, nadac_per_unit). "
             "Max 200 rows.",
             obj({"sql": {"type": "string"}}, ["sql"]),
             run_sql),
    ]
    return {t.name: t for t in tools}


def call_tool(tools: dict[str, Tool], name: str, arguments: str | dict) -> tuple[str, bool, int]:
    """Run a tool call from the model. Returns (json result for the model, ok, row count).

    Errors are returned to the model as data so it can correct itself; they never raise.
    """
    tool = tools.get(name)
    if tool is None:
        return json.dumps({"ok": False, "error": f"unknown tool {name}"}), False, 0
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if not isinstance(args, dict):
            raise ToolError("arguments must be a JSON object")
        rows = tool.fn(**args)
    except (ToolError, SqlRejected) as e:
        return json.dumps({"ok": False, "error": str(e)}), False, 0
    except TypeError as e:
        return json.dumps({"ok": False, "error": f"bad arguments: {e}"}), False, 0
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "arguments were not valid JSON"}), False, 0
    except Exception as e:  # database errors, timeouts
        return json.dumps({"ok": False, "error": f"query failed: {type(e).__name__}: {str(e)[:300]}"}), False, 0
    truncated = len(rows) > MAX_ROWS_TO_MODEL
    payload = {"ok": True, "row_count": len(rows), "rows": rows[:MAX_ROWS_TO_MODEL]}
    if truncated:
        payload["note"] = f"showing first {MAX_ROWS_TO_MODEL} of {len(rows)} rows"
    return json.dumps(payload, default=str), True, len(rows)

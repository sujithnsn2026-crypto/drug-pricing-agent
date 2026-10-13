"""A small, explicit tool-calling agent loop over MCP.

Kept hand-written on purpose: every step (model call, tool call, result) is
recorded in a trace, which the evals and the review UI read.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dpa.ai.llm import ChatModel, Usage


@dataclass
class Step:
    tool: str
    arguments: dict
    ok: bool
    ms: int
    preview: str


@dataclass
class AgentResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    seconds: float = 0.0
    stopped: str = "answer"  # answer | max_steps

    def tools_used(self) -> list[str]:
        return [s.tool for s in self.steps]

    def to_dict(self) -> dict:
        return {"answer": self.answer, "stopped": self.stopped, "seconds": round(self.seconds, 2),
                "usage": vars(self.usage), "steps": [vars(s) for s in self.steps]}


def mcp_tools_to_openai(tools) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": (t.description or "")[:1000],
        "parameters": t.input_schema or {"type": "object", "properties": {}}}} for t in tools]


def _text(result) -> str:
    parts = [c.text for c in (result.content or []) if getattr(c, "type", "") == "text"]
    return "\n".join(parts) if parts else json.dumps(result.structured_content or {})


class Agent:
    def __init__(self, llm: ChatModel, session: ClientSession, system: str,
                 allowed_tools: set[str] | None = None, max_steps: int = 8):
        self.llm, self.session, self.system = llm, session, system
        self.allowed, self.max_steps = allowed_tools, max_steps
        self._tools: list[dict] | None = None

    async def tools(self) -> list[dict]:
        if self._tools is None:
            listed = (await self.session.list_tools()).tools
            if self.allowed is not None:
                listed = [t for t in listed if t.name in self.allowed]
            self._tools = mcp_tools_to_openai(listed)
        return self._tools

    async def run(self, user: str, history: list[dict] | None = None) -> AgentResult:
        t0 = time.time()
        res = AgentResult(answer="")
        messages = [{"role": "system", "content": self.system}, *(history or []), {"role": "user", "content": user}]
        tools = await self.tools()
        names = {t["function"]["name"] for t in tools}
        for _ in range(self.max_steps):
            msg, usage = await self.llm.complete(messages, tools)
            res.usage.add(usage)
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                res.answer = msg.get("content") or ""
                break
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                s0 = time.time()
                if name not in names:
                    out, ok = json.dumps({"error": f"Unknown tool {name}."}), False
                else:
                    try:
                        r = await self.session.call_tool(name, args)
                        out = _text(r)
                        ok = not getattr(r, "is_error", False) and '"error"' not in out[:40]
                    except Exception as e:  # noqa: BLE001
                        out, ok = json.dumps({"error": str(e)[:400]}), False
                res.steps.append(Step(name, args, ok, int((time.time() - s0) * 1000), out[:300]))
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out[:12000]})
        else:
            res.stopped = "max_steps"
            res.answer = res.answer or "I could not finish within the step limit."
        res.seconds = time.time() - t0
        return res


@contextlib.asynccontextmanager
async def mcp_session(env: dict[str, str] | None = None):
    """Start dpa-mcp as a subprocess (stdio transport) and yield a connected client session."""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "dpa.ai.mcp_server"],
        env={**os.environ, "PYTHONPATH": os.path.abspath(src), **(env or {})},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session

"""An agent loop that gets its tools from an MCP server (dpa-mcp) instead of in-process.

Uses the Ask agent's model clients (dpa.agent.llm): live Azure OpenAI, record/replay, or
scripted for tests. Tool calls in one model turn run concurrently, and every step is traced
with OpenTelemetry (dpa.llmops.tracing), so Price-Watch runs show up in App Insights next to
Ask-agent traces.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dpa.agent.llm import LLMClient, Usage
from dpa.llmops.tracing import get_tracer


@dataclass
class Step:
    tool: str
    arguments: dict
    ok: bool
    ms: int


@dataclass
class RunResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model_calls: int = 0
    seconds: float = 0.0
    stopped: str = "answer"


def mcp_tools_to_openai(tools) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:1024],
                                              "parameters": t.input_schema or {"type": "object", "properties": {}}}}
            for t in tools]


def _text(result) -> str:
    parts = [c.text for c in (result.content or []) if getattr(c, "type", "") == "text"]
    return "\n".join(parts) if parts else json.dumps(result.structured_content or {})


class MCPAgent:
    def __init__(self, llm: LLMClient, session: ClientSession, system: str, model: str,
                 allowed_tools: set[str] | None = None, max_steps: int = 8, max_output_tokens: int = 1500):
        self.llm, self.session, self.system, self.model = llm, session, system, model
        self.allowed, self.max_steps, self.max_output_tokens = allowed_tools, max_steps, max_output_tokens
        self._tools: list[dict] | None = None

    async def tools(self) -> list[dict]:
        if self._tools is None:
            listed = (await self.session.list_tools()).tools
            if self.allowed is not None:
                listed = [t for t in listed if t.name in self.allowed]
            self._tools = mcp_tools_to_openai(listed)
        return self._tools

    async def _call(self, name: str, raw_args: str, names: set[str]) -> tuple[str, Step]:
        t0 = time.perf_counter()
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            args = {}
        if name not in names:
            out = json.dumps({"ok": False, "error": f"unknown tool {name}"})
        else:
            try:
                out = _text(await self.session.call_tool(name, args))
            except Exception as e:  # noqa: BLE001
                out = json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"})
        ok = '"ok": true' in out[:30] or '"ok":true' in out[:30]
        return out, Step(name, args, ok, int((time.perf_counter() - t0) * 1000))

    async def run(self, user: str) -> RunResult:
        tracer = get_tracer()
        t0 = time.time()
        res = RunResult(answer="")
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        tools = await self.tools()
        names = {t["function"]["name"] for t in tools}
        with tracer.start_as_current_span("mcp_agent.run") as span:
            span.set_attribute("gen_ai.request.model", self.model)
            for _ in range(self.max_steps):
                resp = await asyncio.to_thread(self.llm.complete, messages, tools, self.model,
                                               None, self.max_output_tokens)
                res.model_calls += 1
                res.usage.input_tokens += resp.usage.input_tokens
                res.usage.output_tokens += resp.usage.output_tokens
                res.usage.cached_input_tokens += resp.usage.cached_input_tokens
                messages.append(resp.as_message())
                if not resp.tool_calls:
                    res.answer = resp.content or ""
                    break
                outs = await asyncio.gather(*(self._call(tc.name, tc.arguments, names) for tc in resp.tool_calls))
                for tc, (out, step) in zip(resp.tool_calls, outs):  # keep the model's call order in the trace
                    res.steps.append(step)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out[:12000]})
            else:
                res.stopped = "max_steps"
            span.set_attribute("gen_ai.usage.input_tokens", res.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", res.usage.output_tokens)
            span.set_attribute("dpa.tool_calls", len(res.steps))
        res.seconds = time.time() - t0
        return res


@contextlib.asynccontextmanager
async def mcp_session(env: dict[str, str] | None = None):
    """Start dpa-mcp as a subprocess over stdio and yield an initialized client session."""
    src = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    params = StdioServerParameters(command=sys.executable, args=["-m", "dpa.ai.mcp_server"],
                                   env={**os.environ, "PYTHONPATH": src, **(env or {})})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session

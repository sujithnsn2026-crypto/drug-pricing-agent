"""The Ask agent: a tool-calling loop with guardrails, tracing and cost accounting.

    question
      -> input guardrail (block injection, no tokens spent)
      -> loop: model -> tool calls (read-only, sanitised results) -> model ...  (max_steps)
      -> number-grounding check -> one self-correction round if it fails
      -> AgentResult (answer, status, steps, tokens, cost, latency, prompt ref, trace id)
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from dpa.agent.llm import LLMClient, Usage
from dpa.agent.tools import Tool, call_tool
from dpa.llmops import guardrails
from dpa.llmops.cost import ModelPrice, cost_usd
from dpa.llmops.prompts import Prompt
from dpa.llmops.tracing import capture_content, get_tracer

AGENT_NAME = "dpa-ask"


@dataclass
class Step:
    kind: str  # "model" | "tool" | "grounding_retry"
    name: str = ""
    arguments: str = ""
    ok: bool = True
    rows: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    sanitized_spans: int = 0
    error: str = ""


@dataclass
class AgentResult:
    question: str
    answer: str
    status: str  # answered | blocked_input | max_steps | error
    model: str
    prompt_ref: str
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = 0.0
    latency_ms: float = 0.0
    grounding_checked: int = 0
    ungrounded_numbers: list[str] = field(default_factory=list)
    grounding_retries: int = 0
    input_block_reason: str = ""
    sanitized_spans: int = 0
    trace_id: str = ""
    error: str = ""

    @property
    def tools_called(self) -> list[str]:
        return [s.name for s in self.steps if s.kind == "tool"]

    @property
    def model_calls(self) -> int:
        return sum(1 for s in self.steps if s.kind == "model")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tools_called"] = self.tools_called
        d["model_calls"] = self.model_calls
        return d


class AskAgent:
    def __init__(self, llm: LLMClient, tools: dict[str, Tool], prompt: Prompt, model: str,
                 prices: dict[str, ModelPrice] | None = None, as_of_date: str = "",
                 max_steps: int = 6, max_grounding_retries: int = 1, tracer: trace.Tracer | None = None):
        self.llm, self.tools, self.prompt, self.model = llm, tools, prompt, model
        self.prices = prices or {}
        self.as_of_date = as_of_date
        self.max_steps = max_steps
        self.max_grounding_retries = max_grounding_retries
        self.tracer = tracer or get_tracer()
        self.tool_specs = [t.spec() for t in tools.values()]

    def ask(self, question: str) -> AgentResult:
        t0 = time.perf_counter()
        res = AgentResult(question=question, answer="", status="answered", model=self.model,
                          prompt_ref=self.prompt.ref)
        with self.tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}") as root:
            res.trace_id = format(root.get_span_context().trace_id, "032x")
            root.set_attributes({
                "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT_NAME,
                "gen_ai.request.model": self.model, "dpa.prompt.id": self.prompt.id,
                "dpa.prompt.version": self.prompt.version, "dpa.prompt.sha": self.prompt.sha,
                "dpa.question.chars": len(question or ""),
            })
            if capture_content():
                root.set_attribute("dpa.question", question)
            try:
                self._run(question, res)
            except Exception as e:  # model/API failure: report it, don't crash the caller
                res.status, res.error = "error", f"{type(e).__name__}: {e}"[:500]
                res.answer = "Sorry, something went wrong while answering. Please try again."
                root.record_exception(e)
                root.set_status(Status(StatusCode.ERROR, res.error))
            res.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            res.cost_usd = cost_usd(res.usage, self.model, self.prices)
            root.set_attributes({
                "dpa.status": res.status,
                "gen_ai.usage.input_tokens": res.usage.input_tokens,
                "gen_ai.usage.output_tokens": res.usage.output_tokens,
                "dpa.usage.cached_input_tokens": res.usage.cached_input_tokens,
                "dpa.cost.usd": res.cost_usd if res.cost_usd is not None else -1.0,
                "dpa.model_calls": res.model_calls,
                "dpa.tool_calls": len(res.tools_called),
                "dpa.tools": ",".join(res.tools_called),
                "dpa.grounding.checked": res.grounding_checked,
                "dpa.grounding.ungrounded": len(res.ungrounded_numbers),
                "dpa.grounding.retries": res.grounding_retries,
                "dpa.guardrail.input_block": res.input_block_reason,
                "dpa.guardrail.sanitized_spans": res.sanitized_spans,
            })
            if capture_content():
                root.set_attribute("dpa.answer", res.answer)
        return res

    # ------------------------------------------------------------------ #
    def _run(self, question: str, res: AgentResult) -> None:
        check = guardrails.check_input(question)
        if check.blocked:
            with self.tracer.start_as_current_span("guardrail input") as s:
                s.set_attributes({"dpa.guardrail.name": "input", "dpa.guardrail.reason": check.reason})
            res.status, res.input_block_reason, res.answer = "blocked_input", check.reason, guardrails.BLOCKED_REPLY
            return

        messages: list[dict] = [
            {"role": "system", "content": self.prompt.render(as_of_date=self.as_of_date)},
            {"role": "user", "content": question},
        ]
        evidence: list[str] = []
        final: str | None = None
        for _ in range(self.max_steps):
            resp = self._model_call(messages, res)
            messages.append(resp.as_message())
            if resp.tool_calls:
                for tc in resp.tool_calls:
                    out = self._tool_call(tc.name, tc.arguments, res)
                    evidence.append(out)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
                continue
            final = (resp.content or "").strip()
            g = self._grounding(final, evidence, question, res)
            if g.ok or res.grounding_retries >= self.max_grounding_retries:
                break
            res.grounding_retries += 1
            res.steps.append(Step("grounding_retry", ok=False, error=",".join(g.ungrounded)))
            messages.append({"role": "user", "content": (
                "Automated check: these numbers in your answer do not appear in any tool result: "
                f"{', '.join(g.ungrounded)}. Do not calculate or estimate. Use a tool to get the exact "
                "figure, or remove the number. Reply with the corrected final answer only.")})
            final = None
        if final is None:
            res.status = "max_steps"
            res.answer = ("I couldn't finish answering within my step limit. Try a narrower question, "
                          "for example about one drug or one time window.")
            return
        res.answer = final

    def _model_call(self, messages: list[dict], res: AgentResult):
        with self.tracer.start_as_current_span(f"chat {self.model}") as s:
            s.set_attributes({"gen_ai.operation.name": "chat", "gen_ai.request.model": self.model,
                              "gen_ai.system": "az.ai.openai"})
            if self.prompt.temperature is not None:
                s.set_attribute("gen_ai.request.temperature", self.prompt.temperature)
            resp = self.llm.complete(messages, self.tool_specs, self.model, self.prompt.temperature)
            s.set_attributes({
                "gen_ai.response.model": resp.model,
                "gen_ai.response.finish_reasons": [resp.finish_reason or ""],
                "gen_ai.usage.input_tokens": resp.usage.input_tokens,
                "gen_ai.usage.output_tokens": resp.usage.output_tokens,
                "dpa.tool_calls_requested": len(resp.tool_calls),
                "dpa.model_latency_ms": resp.latency_ms,
            })
        res.usage.input_tokens += resp.usage.input_tokens
        res.usage.output_tokens += resp.usage.output_tokens
        res.usage.cached_input_tokens += resp.usage.cached_input_tokens
        res.steps.append(Step("model", name=resp.model, latency_ms=resp.latency_ms,
                              input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens))
        return resp

    def _tool_call(self, name: str, arguments: str, res: AgentResult) -> str:
        t0 = time.perf_counter()
        with self.tracer.start_as_current_span(f"execute_tool {name}") as s:
            s.set_attributes({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                              "dpa.tool.arguments": arguments[:1000]})
            out, ok, rows = call_tool(self.tools, name, arguments)
            out, removed = guardrails.sanitize_tool_output(out)
            err = "" if ok else json.loads(out).get("error", "")
            s.set_attributes({"dpa.tool.ok": ok, "dpa.tool.rows": rows, "dpa.guardrail.sanitized_spans": removed})
            if not ok:
                s.set_status(Status(StatusCode.ERROR, err[:200]))
        res.sanitized_spans += removed
        res.steps.append(Step("tool", name=name, arguments=arguments, ok=ok, rows=rows,
                              latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                              sanitized_spans=removed, error=err))
        return out

    def _grounding(self, answer: str, evidence: list[str], question: str, res: AgentResult):
        with self.tracer.start_as_current_span("guardrail number_grounding") as s:
            g = guardrails.check_grounding(answer, evidence, question)
            s.set_attributes({"dpa.guardrail.name": "number_grounding", "dpa.grounding.checked": g.checked,
                              "dpa.grounding.ungrounded": len(g.ungrounded)})
        res.grounding_checked, res.ungrounded_numbers = g.checked, g.ungrounded
        return g

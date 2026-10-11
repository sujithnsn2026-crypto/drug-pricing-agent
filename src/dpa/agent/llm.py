"""Model clients behind one small interface.

- AzureOpenAIClient   the real thing: Azure OpenAI chat completions with tool calling
- RecordingClient     wraps a real client and saves every call to a cassette file
- ReplayClient        answers from a cassette; no network, no cost, deterministic
- ScriptedClient      hand-written responses for unit tests

Record/replay is what lets CI run the full eval suite on every push for free:
a live run records the model's responses once, and CI replays them. If a
prompt, tool schema or tool result changes, the request hash changes, the
cassette misses, and CI says "re-record with a live run" instead of silently
passing on stale answers.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, exactly as the model produced it


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LLMResponse":
        return cls(
            content=d.get("content"),
            tool_calls=[ToolCall(**tc) for tc in d.get("tool_calls", [])],
            usage=Usage(**d.get("usage", {})),
            model=d.get("model", ""),
            finish_reason=d.get("finish_reason", ""),
            latency_ms=d.get("latency_ms", 0.0),
        )

    def as_message(self) -> dict:
        """The assistant turn to append to the conversation."""
        msg: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return msg


class LLMClient(Protocol):
    def complete(self, messages: list[dict], tools: list[dict], model: str,
                 temperature: float | None = None, max_output_tokens: int = 1200) -> LLMResponse: ...


# --------------------------------------------------------------------------- #
# Azure OpenAI
# --------------------------------------------------------------------------- #
class AzureOpenAIClient:
    """Azure OpenAI through the v1 API (`<endpoint>/openai/v1/`).

    `model` is the Azure *deployment* name. Auth: AZURE_OPENAI_API_KEY if set,
    otherwise Microsoft Entra ID via azure-identity (DefaultAzureCredential),
    which is what you'd use in production with a managed identity.
    """

    def __init__(self, endpoint: str | None = None, api_key: str | None = None, timeout: float = 60.0):
        from openai import OpenAI

        endpoint = (endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")).rstrip("/")
        if not endpoint:
            raise RuntimeError("Set AZURE_OPENAI_ENDPOINT (e.g. https://<name>.openai.azure.com)")
        api_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if not api_key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            api_key = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        self._client = OpenAI(base_url=f"{endpoint}/openai/v1/", api_key=api_key, timeout=timeout,
                              max_retries=3)

    def complete(self, messages, tools, model, temperature=None, max_output_tokens=1200) -> LLMResponse:
        kwargs: dict = {"model": model, "messages": messages, "max_completion_tokens": max_output_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = True
        if temperature is not None:
            kwargs["temperature"] = temperature
        t0 = time.perf_counter()
        r = self._client.chat.completions.create(**kwargs)
        latency = (time.perf_counter() - t0) * 1000
        choice = r.choices[0]
        u = r.usage
        cached = 0
        if u is not None and getattr(u, "prompt_tokens_details", None) is not None:
            cached = getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0
        return LLMResponse(
            content=choice.message.content,
            tool_calls=[ToolCall(tc.id, tc.function.name, tc.function.arguments)
                        for tc in (choice.message.tool_calls or [])],
            usage=Usage(u.prompt_tokens if u else 0, u.completion_tokens if u else 0, cached),
            model=r.model or model,
            finish_reason=choice.finish_reason or "",
            latency_ms=round(latency, 1),
        )


# --------------------------------------------------------------------------- #
# Record / replay
# --------------------------------------------------------------------------- #
class CassetteMiss(RuntimeError):
    pass


def request_key(messages, tools, model, temperature, max_output_tokens) -> str:
    canon = json.dumps({"m": messages, "t": tools, "model": model, "temp": temperature,
                        "max": max_output_tokens}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def _normalise_ids(messages: list[dict]) -> list[dict]:
    """Tool-call IDs are random per live call; replace them with stable ones before hashing."""
    mapping: dict[str, str] = {}
    out = []
    for m in messages:
        m = json.loads(json.dumps(m))
        for tc in m.get("tool_calls") or []:
            mapping.setdefault(tc["id"], f"call_{len(mapping)}")
            tc["id"] = mapping[tc["id"]]
        if m.get("role") == "tool" and m.get("tool_call_id") in mapping:
            m["tool_call_id"] = mapping[m["tool_call_id"]]
        out.append(m)
    return out


class _Cassette:
    def __init__(self, path: str):
        self.path = path
        self.entries: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if line.strip():
                        e = json.loads(line)
                        self.entries[e["key"]] = e["response"]


class RecordingClient:
    def __init__(self, inner: LLMClient, path: str):
        self.inner, self.cassette = inner, _Cassette(path)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def complete(self, messages, tools, model, temperature=None, max_output_tokens=1200) -> LLMResponse:
        r = self.inner.complete(messages, tools, model, temperature, max_output_tokens)
        key = request_key(_normalise_ids(messages), tools, model, temperature, max_output_tokens)
        with self._lock:
            if key not in self.cassette.entries:
                self.cassette.entries[key] = r.to_dict()
                with open(self.cassette.path, "a") as f:
                    f.write(json.dumps({"key": key, "response": r.to_dict()}) + "\n")
        return r


class ReplayClient:
    def __init__(self, path: str):
        if not os.path.exists(path):
            raise FileNotFoundError(f"no cassette at {path}; record one with a live eval run (--record)")
        self.cassette = _Cassette(path)

    def complete(self, messages, tools, model, temperature=None, max_output_tokens=1200) -> LLMResponse:
        key = request_key(_normalise_ids(messages), tools, model, temperature, max_output_tokens)
        if key not in self.cassette.entries:
            raise CassetteMiss(
                "request not in cassette: the prompt, tools, model or tool results changed since it was "
                "recorded. Re-record with a live eval run.")
        return LLMResponse.from_dict(self.cassette.entries[key])


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
class ScriptedClient:
    """Returns responses from a function of the conversation, for unit tests."""

    def __init__(self, script: Callable[[list[dict]], LLMResponse]):
        self.script = script
        self.calls: list[list[dict]] = []

    def complete(self, messages, tools, model, temperature=None, max_output_tokens=1200) -> LLMResponse:
        self.calls.append(json.loads(json.dumps(messages)))
        r = self.script(messages)
        r.model = r.model or model
        return r

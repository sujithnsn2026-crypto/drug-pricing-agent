"""Model clients. Azure OpenAI (deployed in Azure AI Foundry) with Entra ID auth,
plus a scripted fake used by the tests so agent logic is tested without an API."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from dpa.ai.settings import AISettings

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls

    def cost_usd(self, s: AISettings) -> float:
        return self.prompt_tokens / 1e6 * s.price_in_per_m + self.completion_tokens / 1e6 * s.price_out_per_m


class ChatModel(Protocol):
    name: str

    async def complete(self, messages: list[dict], tools: list[dict] | None) -> tuple[dict, Usage]: ...


def _auth(s: AISettings) -> dict:
    """API key if AZURE_OPENAI_API_KEY is set (read at runtime, never stored in code);
    otherwise Entra ID (needs the 'Cognitive Services OpenAI User' role)."""
    if s.aoai_api_key:
        return {"api_key": s.aoai_api_key}
    return {"azure_ad_token_provider": _token_provider()}


def _token_provider():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(exclude_interactive_browser_credential=True), COGNITIVE_SCOPE)


class AzureChat:
    def __init__(self, s: AISettings, deployment: str | None = None):
        from openai import AsyncAzureOpenAI

        if not (s.aoai_endpoint and (deployment or s.chat_deployment)):
            raise RuntimeError("Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_CHAT_DEPLOYMENT.")
        self.name = deployment or s.chat_deployment
        self.client = AsyncAzureOpenAI(azure_endpoint=s.aoai_endpoint, api_version=s.aoai_api_version,
                                       max_retries=4, **_auth(s))

    async def complete(self, messages, tools):
        kw: dict[str, Any] = {"model": self.name, "messages": messages}
        if tools:
            kw["tools"] = tools
        r = await self.client.chat.completions.create(**kw)
        m = r.choices[0].message
        msg: dict[str, Any] = {"role": "assistant", "content": m.content}
        if m.tool_calls:
            msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                 for tc in m.tool_calls]
        u = r.usage
        return msg, Usage(u.prompt_tokens if u else 0, u.completion_tokens if u else 0, 1)


def embedder(s: AISettings) -> Callable[[list[str]], list[list[float]]]:
    from openai import AzureOpenAI

    client = AzureOpenAI(azure_endpoint=s.aoai_endpoint, api_version=s.aoai_api_version,
                         max_retries=6, **_auth(s))

    def embed(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            r = client.embeddings.create(model=s.embed_deployment, input=texts[i:i + 64], dimensions=s.embed_dimensions)
            out.extend(d.embedding for d in r.data)
        return out

    return embed


# --------------------------------------------------------------------------- #
# Test double
# --------------------------------------------------------------------------- #
@dataclass
class ScriptedChat:
    """Replays a fixed list of assistant turns. A turn is either a final text answer
    (str) or a list of (tool_name, arguments) calls."""
    script: list[Any]
    name: str = "scripted"
    seen: list[list[dict]] = field(default_factory=list)

    async def complete(self, messages, tools):
        self.seen.append(list(messages))
        turn = self.script.pop(0) if self.script else "(no more scripted turns)"
        if isinstance(turn, str):
            return {"role": "assistant", "content": turn}, Usage(100, 20, 1)
        calls = [{"id": f"call_{len(self.seen)}_{i}", "type": "function",
                  "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(turn)]
        return {"role": "assistant", "content": None, "tool_calls": calls}, Usage(100, 20, 1)

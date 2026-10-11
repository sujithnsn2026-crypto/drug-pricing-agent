"""Build an AskAgent from a few settings, for the CLI, the eval runner and (later) the API."""
from __future__ import annotations

import os
import re

from dpa.agent.agent import AskAgent
from dpa.agent.data import DEFAULT_FIXTURE, DuckDbGold
from dpa.agent.llm import AzureOpenAIClient, LLMClient, RecordingClient, ReplayClient
from dpa.agent.tools import build_tools
from dpa.llmops.cost import load_prices
from dpa.llmops.prompts import load_prompt

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CASSETTE_DIR = os.path.join(ROOT, "evals", "cassettes")
DEFAULT_MODEL = os.environ.get("DPA_MODEL", "gpt-4.1-mini")
MODES = ("live", "record", "replay")


def cassette_path(model: str, prompt_version: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    return os.path.join(CASSETTE_DIR, f"{safe}__ask_agent-{prompt_version}.jsonl")


def make_llm(mode: str, model: str, prompt_version: str, cassette: str | None = None) -> LLMClient:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    path = cassette or cassette_path(model, prompt_version)
    if mode == "replay":
        return ReplayClient(path)
    live = AzureOpenAIClient()
    return RecordingClient(live, path) if mode == "record" else live


def make_agent(mode: str = "live", model: str = DEFAULT_MODEL, prompt_version: str | None = None,
               gold_dir: str = DEFAULT_FIXTURE, cassette: str | None = None, db: DuckDbGold | None = None,
               llm: LLMClient | None = None) -> AskAgent:
    prompt = load_prompt("ask_agent", prompt_version)
    db = db or DuckDbGold(gold_dir)
    llm = llm or make_llm(mode, model, prompt.version, cassette)
    return AskAgent(llm, build_tools(db), prompt, model, prices=load_prices(), as_of_date=db.as_of_date())

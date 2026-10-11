"""Cost per answer from token usage.

Prices live in config/model_prices.yaml (USD per 1M tokens), keyed by the
Azure deployment name. They are inputs you maintain, not facts the code knows:
check the Azure OpenAI pricing page for your region and deployment type and
update the file, including its `checked` date.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

from dpa.agent.llm import Usage

PRICES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "config", "model_prices.yaml")


@dataclass(frozen=True)
class ModelPrice:
    input_per_m: float
    cached_input_per_m: float
    output_per_m: float


def load_prices(path: str = PRICES_FILE) -> dict[str, ModelPrice]:
    with open(path) as f:
        doc = yaml.safe_load(f)
    return {name: ModelPrice(p["input"], p.get("cached_input", p["input"]), p["output"])
            for name, p in doc["models"].items()}


def cost_usd(usage: Usage, model: str, prices: dict[str, ModelPrice]) -> float | None:
    """None when the model has no price entry (reported as 'unknown', never as 0)."""
    p = prices.get(model)
    if p is None:
        return None
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    return (uncached * p.input_per_m + usage.cached_input_tokens * p.cached_input_per_m
            + usage.output_tokens * p.output_per_m) / 1_000_000

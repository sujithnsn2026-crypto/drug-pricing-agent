"""Prompt registry: prompts are versioned files, not strings in code.

    prompts/<prompt_id>/<version>.yaml     one immutable file per version
    prompts/registry.yaml                  which version is live

Every trace and eval result records the prompt id, version and a content hash,
so any answer can be tied back to the exact prompt that produced it, and a
changed-but-not-renamed prompt is caught (the hash moves, the version doesn't).
A new prompt version ships the same way as code: open a PR, the eval gate runs,
merge only if it passes, then flip registry.yaml.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import yaml

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "prompts")


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    temperature: float | None
    sha: str  # first 12 hex chars of sha256 over the system text + settings

    def render(self, **values) -> str:
        return self.system.format(**values)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}#{self.sha}"


def load_prompt(prompt_id: str, version: str | None = None, prompts_dir: str = PROMPTS_DIR) -> Prompt:
    if version is None:
        with open(os.path.join(prompts_dir, "registry.yaml")) as f:
            version = yaml.safe_load(f)["active"][prompt_id]
    path = os.path.join(prompts_dir, prompt_id, f"{version}.yaml")
    with open(path) as f:
        doc = yaml.safe_load(f)
    if doc.get("id") != prompt_id or doc.get("version") != version:
        raise ValueError(f"{path}: id/version inside the file must match its path")
    temperature = (doc.get("settings") or {}).get("temperature")
    payload = f"{doc['system']}\n--\ntemperature={temperature}"
    sha = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return Prompt(prompt_id, version, doc["system"], temperature, sha)


def list_versions(prompt_id: str, prompts_dir: str = PROMPTS_DIR) -> list[str]:
    folder = os.path.join(prompts_dir, prompt_id)
    return sorted(f[:-5] for f in os.listdir(folder) if f.endswith(".yaml"))

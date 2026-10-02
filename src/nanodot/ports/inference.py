"""Inference provider port (see docs/design/adapter-seam.md, port 4).

The model behind this port is the ONLY egress point in nanodot. Requests
are built by core.egress from a fixed whitelist — PR metadata and evidence
excerpts; never credentials, tokens, task-store contents, or memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    """Provider unavailable or returned something unusable. Callers must
    degrade gracefully — the watch never depends on the model."""


@dataclass(frozen=True)
class StateChange:
    """What changed on a watch — the whitelisted evidence a summary is
    built from."""

    kind: str
    summary: str
    head_sha: str = ""
    pr_state: str = ""
    url: str = ""
    checks: tuple[tuple[str, str | None], ...] = ()  # (name, conclusion)


@dataclass(frozen=True)
class TaskDraft:
    """Intent parsing result for `watch add`."""

    target: str = ""
    purpose: str = ""
    raw: str = ""


class InferenceProvider(Protocol):
    def summarize(self, change: StateChange) -> str:
        """One or two sentences on what changed and what it means."""

    def parse_intent(self, text: str) -> TaskDraft:
        """Turn a user sentence into a task draft (target + purpose)."""

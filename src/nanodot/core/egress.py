"""Egress whitelist — what may leave the host through the inference port.

Requests are built here, from explicit arguments, into fixed shapes. There
is deliberately no way to add fields: anything not in the whitelist cannot
be attached, and known secret values are scrubbed from the values that do
leave. See docs/design/egress.md.
"""

from __future__ import annotations

from nanodot.core.redaction import Redactor
from nanodot.ports.inference import StateChange

SUMMARIZE_FIELDS = ("kind", "summary", "head_sha", "pr_state", "url", "checks")
INTENT_FIELDS = ("intent_text",)


class EgressGuard:
    def __init__(self, redactor: Redactor | None = None) -> None:
        self._redactor = redactor

    def _scrub(self, text: str) -> str:
        return self._redactor.scrub(text) if self._redactor else text

    def summarize_request(self, change: StateChange) -> dict:
        """Exactly the whitelisted fields, scrubbed — nothing else exists
        to attach."""
        return {
            "kind": change.kind,
            "summary": self._scrub(change.summary),
            "head_sha": change.head_sha,
            "pr_state": change.pr_state,
            "url": change.url,
            "checks": [
                {"name": name, "conclusion": conclusion}
                for name, conclusion in change.checks
            ],
        }

    def intent_request(self, text: str) -> dict:
        """User-authored input only."""
        return {"intent_text": self._scrub(text)}

"""Egress whitelist — what may leave the host through the inference port.

Requests are built here, from explicit arguments, into fixed shapes. There
is deliberately no way to add fields: anything not in the whitelist cannot
be attached, and known secret values are scrubbed from the values that do
leave. See docs/design/egress.md.
"""

from __future__ import annotations

from collections.abc import Callable

from nanodot.core.redaction import Redactor
from nanodot.ports.inference import StateChange

SUMMARIZE_FIELDS = ("kind", "summary", "head_sha", "pr_state", "url", "checks")
INTENT_FIELDS = ("intent_text",)


class EgressGuard:
    def __init__(
        self, redactor: Redactor | None = None,
        scrubber: Callable[[str], str] | None = None,
    ) -> None:
        self._redactor = redactor
        self._scrubber = scrubber

    def _scrub(self, text: str) -> str:
        text = self._redactor.scrub(text) if self._redactor else text
        return self._scrubber(text) if self._scrubber else text

    def summarize_request(self, change: StateChange) -> dict:
        """Exactly the whitelisted fields, scrubbed — nothing else exists
        to attach."""
        payload = {
            "kind": self._scrub(change.kind),
            "summary": self._scrub(change.summary),
            "head_sha": self._scrub(change.head_sha),
            "pr_state": self._scrub(change.pr_state),
            "url": self._scrub(change.url),
            "checks": [
                {"name": self._scrub(name),
                 "conclusion": self._scrub(conclusion) if conclusion is not None else None}
                for name, conclusion in change.checks
            ],
        }
        return payload

    def intent_request(self, text: str) -> dict:
        """User-authored input only."""
        return {"intent_text": self._scrub(text)}

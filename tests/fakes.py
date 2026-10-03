"""Test doubles — the substitution side of the adapter seam.

Fakes drive the whole task loop in unit and e2e tests; the same interfaces
a future adapter would implement (docs/design/adapter-seam.md).
"""

from __future__ import annotations

import hashlib
import json
import time

from nanodot.core.tasks import PRTarget
from nanodot.ports.github import (
    AuthLostError,
    CheckRun,
    PRNotFoundError,
    RetryableError,
    RequiredCheck,
    Snapshot,
    SnapshotFetcher,
)
from nanodot.ports.github_writer import (
    WriteContentMismatch,
    WriteResult,
)
from nanodot.ports.inference import ProviderError, TaskDraft

SUCCESS = "success"
FAILURE = "failure"
QUEUED = "queued"
COMPLETED = "completed"


class FakeGitHub(SnapshotFetcher):
    """Scriptable GitHub: set PR state, per-SHA check runs, or errors."""

    def __init__(self, target: PRTarget) -> None:
        self.target = target
        self.pr_state = "open"
        self.head_sha = "sha-1"
        self.checks: dict[str, list[CheckRun]] = {}
        self.error: Exception | None = None
        self.fetch_calls = 0

    def set_pr(self, state: str, head_sha: str | None = None) -> None:
        self.pr_state = state
        if head_sha:
            self.head_sha = head_sha

    def add_check(
        self, name: str, conclusion: str | None, sha: str, status: str = COMPLETED
    ) -> None:
        self.checks.setdefault(sha, []).append(
            CheckRun(name=name, status=status, conclusion=conclusion, sha=sha)
        )

    def fail_with(self, error: Exception | None) -> None:
        self.error = error

    def snapshot(self) -> Snapshot:
        self.fetch_calls += 1
        return Snapshot(
            target=self.target,
            pr_state=self.pr_state,
            head_sha=self.head_sha,
            checks=tuple(self.checks.get(self.head_sha, ()))
            + tuple(
                run
                for sha, runs in self.checks.items()
                if sha != self.head_sha
                for run in runs
            ),
            required_checks=tuple(
                RequiredCheck(name=run.name)
                for run in self.checks.get(self.head_sha, ())
            ),
            checks_complete=True,
            fetched_at=time.time(),
            url=f"https://github.com/{self.target.owner}/{self.target.repo}"
            f"/pull/{self.target.number}",
        )

    def fetch(self, target: PRTarget) -> Snapshot:
        if self.error is not None:
            raise self.error
        return self.snapshot()


class FakeProvider:
    """Scriptable inference provider: records egress, returns canned
    answers, can be told to fail (degraded-mode tests)."""

    def __init__(self) -> None:
        self.summarize_payloads: list = []
        self.intent_payloads: list = []
        self.summaries: list[str] = ["A short model summary."]
        self.fail_summaries = False
        self.fail_intent = False
        self.draft = TaskDraft(target="owner/repo#1", purpose="watch checks")

    def summarize(self, change) -> str:
        self.summarize_payloads.append(change)
        if self.fail_summaries:
            raise ProviderError("provider down")
        return self.summaries[0]

    def parse_intent(self, text: str) -> TaskDraft:
        self.intent_payloads.append(text)
        if self.fail_intent:
            raise ProviderError("provider down")
        return self.draft


class FakeSink:
    """Records every notified event for assertions."""

    def __init__(self) -> None:
        self.events: list = []

    def notify(self, event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


class FakeClock:
    """Deterministic clock for cadence/backoff/expiry tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


TYPICAL_ERRORS = {
    "rate-limit": RetryableError("rate limit"),
    "auth": AuthLostError("token revoked"),
    "missing": PRNotFoundError("PR deleted"),
}


class FakeGitHubWriter:
    """Scriptable writer: records verified requests, returns canned results.

    Mirrors the port contract — the content hash is enforced exactly like
    the native adapter, so hash-mismatch cases fail closed here too.
    """

    def __init__(self) -> None:
        self.executed: list[tuple] = []  # (capability, payload, sent_bytes)
        self.results: list = []  # WriteResult | WriteError, consumed in order
        self.hash_checks = 0

    def respond_with(self, *outcomes) -> None:
        self.results.extend(outcomes)

    def execute(self, capability, payload):
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self.hash_checks += 1
        if hashlib.sha256(canonical.encode()).hexdigest() != capability.content_hash:
            raise WriteContentMismatch("payload does not match the approved hash")
        self.executed.append((capability, payload, canonical.encode()))
        outcome = self.results.pop(0) if self.results else WriteResult(
            action=capability.action, status=201, body={"ok": True}
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

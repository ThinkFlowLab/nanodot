"""Test doubles — the substitution side of the adapter seam.

Fakes drive the whole task loop in unit and e2e tests; the same interfaces
a future adapter would implement (docs/design/adapter-seam.md).
"""

from __future__ import annotations

import time

from nanodot.core.tasks import PRTarget
from nanodot.ports.github import (
    AuthLostError,
    CheckRun,
    PRNotFoundError,
    RetryableError,
    Snapshot,
    SnapshotFetcher,
)

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
            fetched_at=time.time(),
            url=f"https://github.com/{self.target.owner}/{self.target.repo}"
            f"/pull/{self.target.number}",
        )

    def fetch(self, target: PRTarget) -> Snapshot:
        if self.error is not None:
            raise self.error
        return self.snapshot()


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

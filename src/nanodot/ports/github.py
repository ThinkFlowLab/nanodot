"""Read-only GitHub snapshot port, with explicit completeness evidence.

Adapters must supply all current check sources and the applicable required
contexts. Omitted/unknown metadata never authorizes a terminal success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nanodot.core.tasks import PRTarget


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    sha: str  # the actual commit reported by the source, never relabeled
    source: str = "check_run"  # check_run | status | check_suite
    app_id: int | None = None
    run_id: int | None = None
    suite_id: int | None = None


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    app_id: int | None = None  # None permits any source; never an inferred ID


@dataclass(frozen=True)
class Snapshot:
    target: PRTarget
    pr_state: str
    head_sha: str
    checks: tuple[CheckRun, ...]
    fetched_at: float
    url: str
    required_checks: tuple[RequiredCheck, ...] | None = None
    checks_complete: bool = False
    # Advisory transport metadata (the tightest X-RateLimit-Remaining seen
    # during the fetch). Never part of snapshot identity: fingerprints and
    # events deliberately ignore it — only observations record it.
    rate_limit_remaining: int | None = None

    def checks_for(self, sha: str) -> tuple[CheckRun, ...]:
        return tuple(run for run in self.checks if run.sha == sha)


class FetchError(Exception):
    """Base class; never persist partial data as a success on these."""


class RetryableError(FetchError):
    """Rate limit, 5xx, network trouble, or inconsistent pages — retry."""


class AuthLostError(FetchError):
    """Invalid/revoked token — surfaces as a task blocker."""


class PRNotFoundError(FetchError):
    """PR deleted or inaccessible — surfaces as a task blocker."""


class SnapshotFetcher(Protocol):
    def fetch(self, target: PRTarget) -> Snapshot: ...

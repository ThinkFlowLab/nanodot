"""GitHub snapshot fetch port (see docs/design/adapter-seam.md, port 2).

Read-only in every implementation: check runs keyed to the commit they ran
against, so passing results on an older SHA can never satisfy a watch on a
newer one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nanodot.core.tasks import PRTarget


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | ... | None while pending
    sha: str  # the commit this run executed against


@dataclass(frozen=True)
class Snapshot:
    target: PRTarget
    pr_state: str  # open | merged | closed
    head_sha: str
    checks: tuple[CheckRun, ...]
    fetched_at: float
    url: str

    def checks_for(self, sha: str) -> tuple[CheckRun, ...]:
        return tuple(run for run in self.checks if run.sha == sha)


class FetchError(Exception):
    """Base class; never persist partial data as a success on these."""


class RetryableError(FetchError):
    """Rate limit, 5xx, or network trouble — back off and retry."""


class AuthLostError(FetchError):
    """Invalid/revoked token — surfaces as a task blocker."""


class PRNotFoundError(FetchError):
    """PR deleted or inaccessible — surfaces as a task blocker."""


class SnapshotFetcher(Protocol):
    def fetch(self, target: PRTarget) -> Snapshot: ...

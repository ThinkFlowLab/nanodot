"""The GitHub writer port — bounded, approved writes (docs/design/github-writer.md).

A write executes only behind a content-bound, single-use capability issued
by ``PermissionCenter.approve()``. The Protocol's surface is frozen by an
acceptance test: exactly ``{execute}`` — there is intentionally no way to
write without a capability, and no capability without a human approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nanodot.core.permissions import WriteCapability


@dataclass(frozen=True)
class WriteOutcome:
    """The result of one attempted write. Errors are outcomes, not
    exceptions: a failed write is a recorded fact, never a crash."""

    ok: bool
    url: str | None = None
    error: str | None = None


@runtime_checkable
class GitHubWriter(Protocol):
    """Transport for approved writes. Implementations must verify the
    capability's ``content_hash`` against the payload before sending and
    refuse anything that does not match (fail closed)."""

    def execute(self, capability: WriteCapability, body: str) -> WriteOutcome:
        ...

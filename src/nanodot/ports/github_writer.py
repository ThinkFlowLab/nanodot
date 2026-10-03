"""The GitHub writer port — bounded, approved writes (docs/design/github-writer.md).

The only seam able to issue an authenticated non-GET request. The port's
single method executes a write through a ``WriteCapability`` — a
human-approved, content-bound, single-use capability — so core can never
reach the wire without an approval, by construction rather than convention.

No production caller exists yet: the credential lands in #52, the
capability store and approvals surface in #55, the first action in #54.
Until then this port exists so the no-external-write invariant can be
pinned to one place — non-GET request construction lives in the write
port's native adapter and nowhere else.

Sequencing note: ``used_at`` is carried by the capability but marked by the
capability store's atomic consume when #55 lands; ``write-intent`` crash
logging joins with the first action (#54). The port surface itself is
frozen here: exactly ``{execute}``, and exactly these capability fields.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Protocol


class WriteError(Exception):
    """Base class for write-port failures; typed, never matched by prose."""


class WriteActionUnknown(WriteError):
    """No such write action — rejected before any credential or transport."""


class WriteCredentialMissing(WriteError):
    """No write credential configured — nothing left the host."""


class WriteContentMismatch(WriteError):
    """Payload does not hash to the approved content hash — fail closed."""


class WriteRejectedError(WriteError):
    """The destination refused the write (4xx)."""


class WriteTransportError(WriteError):
    """Network trouble, redirect attempt, or 5xx — retryable at best."""


def payload_digest(payload: dict) -> str:
    """The content hash approvals bind: canonical JSON of the exact payload.

    The same serialization becomes the request body, so the bytes at the
    HTTP boundary hash-equal the approved hash.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class WriteCapability:
    """A single-use, content-bound capability issued only by an approval.

    ``content_hash`` is ``payload_digest`` of the exact approved payload;
    execution verifies it before anything is sent, so approved-then-mutated
    content fails closed. ``used_at`` is None until the capability store's
    atomic consume marks it (#55).
    """

    action: str
    target: str
    content_hash: str
    grant_id: str
    used_at: float | None = None


CAPABILITY_FIELDS = frozenset(f.name for f in fields(WriteCapability))


@dataclass(frozen=True)
class WriteResult:
    action: str
    status: int
    body: dict = field(default_factory=dict)


class GitHubWriter(Protocol):
    """Executes one approved write: verify hash, send, map typed errors.

    Implementations raise WriteCredentialMissing before any socket use when
    no write credential is configured, never follow redirects for
    authenticated requests, and use only the write credential — never the
    read token.
    """

    def execute(self, capability: WriteCapability, payload: dict) -> WriteResult: ...

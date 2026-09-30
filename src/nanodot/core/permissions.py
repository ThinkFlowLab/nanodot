"""Permissions — ZCode-style (issue #1 review, 2026-09-30).

Named modes with `readonly` as the enforced MVP default; approvals are
persisted, inspectable, revocable grants scoped to action type + target +
scope + expiry; silence is never approval (unanswered requests expire and
the task stays blocked); denials are recorded; grants are invalidated when
task scope changes. Belt and suspenders: the mode governs what nanodot
attempts, the read-only PAT caps what it could do — a bug cannot write.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nanodot.core.config import Config
from nanodot.paths import database_path

DEFAULT_REQUEST_TTL_SECONDS = 24 * 3600


class WriteForbidden(PermissionError):
    """Raised when code attempts a write action under the readonly mode."""


class Mode(str, Enum):
    READONLY = "readonly"
    GATED = "gated"  # designed, dormant: write actions would pause for approval
    AUTO = "auto"    # designed, dormant: pre-granted scoped capabilities only


READ_ACTIONS = frozenset({"read"})


@dataclass(frozen=True)
class Grant:
    id: str
    action: str
    target: str
    scope: str
    task_id: str
    created_at: float
    expires_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    action: str
    target: str
    scope: str
    task_id: str
    created_at: float
    expires_at: float
    state: str  # pending | approved | denied | expired


_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  scope TEXT NOT NULL,
  task_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL
);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  scope TEXT NOT NULL,
  task_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  state TEXT NOT NULL
);
"""


def _now() -> float:
    return time.time()


class PermissionCenter:
    def __init__(self, path: Path | None = None, clock=time) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- mode ---------------------------------------------------------------

    def mode(self) -> Mode:
        try:
            return Mode(str(Config().get("mode", Mode.READONLY.value)))
        except ValueError:
            return Mode.READONLY

    def assert_allowed(self, action: str) -> None:
        """The gate every external action must pass. In the read-only MVP
        this raises for any non-read action — there are no write paths."""
        if action in READ_ACTIONS:
            return
        if self.mode() is Mode.READONLY:
            raise WriteForbidden(
                f"action {action!r} is a write; nanodot is in readonly mode"
            )

    # -- requests -----------------------------------------------------------

    def request(
        self,
        action: str,
        target: str,
        scope: str,
        task_id: str,
        ttl: float = DEFAULT_REQUEST_TTL_SECONDS,
    ) -> ApprovalRequest:
        """Record what action is wanted, showing action/target/scope/effect.
        Silence never approves: it expires."""
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:12],
            action=action,
            target=target,
            scope=scope,
            task_id=task_id,
            created_at=self._clock.time(),
            expires_at=self._clock.time() + ttl,
            state="pending",
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?)",
                (
                    req.id, req.action, req.target, req.scope, req.task_id,
                    req.created_at, req.expires_at, req.state,
                ),
            )
            self._conn.commit()
        return req

    def approve(self, request_id: str) -> Grant:
        """Approval creates a grant scoped to exactly this action + target +
        scope, with an expiry. Nothing broader."""
        req = self._require_request(request_id)
        if req.state == "expired":
            raise ValueError("request expired — silence is not approval; re-request")
        if req.state != "pending":
            raise ValueError(f"request is already {req.state}")
        grant = Grant(
            id=uuid.uuid4().hex[:12],
            action=req.action,
            target=req.target,
            scope=req.scope,
            task_id=req.task_id,
            created_at=self._clock.time(),
            expires_at=req.expires_at,
            revoked_at=None,
        )
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET state='approved' WHERE id=?", (request_id,)
            )
            self._conn.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?)",
                (
                    grant.id, grant.action, grant.target, grant.scope,
                    grant.task_id, grant.created_at, grant.expires_at, None,
                ),
            )
            self._conn.commit()
        return grant

    def deny(self, request_id: str) -> None:
        """A denial is an answer: recorded, never re-asked verbatim."""
        self._transition_request(request_id, "denied")

    def sweep_expired(self) -> int:
        now = self._clock.time()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE requests SET state='expired' "
                "WHERE state='pending' AND expires_at <= ?",
                (now,),
            )
            self._conn.commit()
            return cursor.rowcount

    def pending(self) -> list[ApprovalRequest]:
        return self._requests_where("state='pending'")

    def request_history(self) -> list[ApprovalRequest]:
        return self._requests_where("1=1")

    # -- grants ---------------------------------------------------------------

    def grants(self, active_only: bool = False) -> list[Grant]:
        where = "revoked_at IS NULL" if active_only else "1=1"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM grants WHERE {where} ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_grant(row) for row in rows]

    def permits(
        self,
        action: str,
        target: str,
        scope: str,
        task_id: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Exact-scope matching: same action on a different target, or a
        different action on the same target, does not match."""
        at = now if now is not None else self._clock.time()
        for grant in self.grants(active_only=True):
            if (
                grant.action == action
                and grant.target == target
                and grant.scope == scope
                and (task_id is None or grant.task_id == task_id)
                and (grant.expires_at is None or grant.expires_at > at)
            ):
                return True
        return False

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE grants SET revoked_at=? WHERE id=?",
                (self._clock.time(), grant_id),
            )
            self._conn.commit()

    def on_scope_change(self, task_id: str) -> int:
        """A task's scope changed: every grant tied to that task dies."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE grants SET revoked_at=? "
                "WHERE task_id=? AND revoked_at IS NULL",
                (self._clock.time(), task_id),
            )
            self._conn.commit()
            return cursor.rowcount

    # -- internals ----------------------------------------------------------------

    def _transition_request(self, request_id: str, state: str) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE requests SET state=? WHERE id=? AND state='pending'",
                (state, request_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                raise ValueError(f"no pending request {request_id}")

    def _require_request(self, request_id: str) -> ApprovalRequest:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"no such request {request_id}")
        return self._row_to_request(row)

    def _requests_where(self, where: str) -> list[ApprovalRequest]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests WHERE {where} ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"],
            action=row["action"],
            target=row["target"],
            scope=row["scope"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            state=row["state"],
        )

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> Grant:
        return Grant(
            id=row["id"],
            action=row["action"],
            target=row["target"],
            scope=row["scope"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    def close(self) -> None:
        self._conn.close()

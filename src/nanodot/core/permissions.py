"""Permissions — ZCode-style (issue #1 review, 2026-09-30; writer ports #59, #48).

Named modes with `auto` as the default: `auto` executes writes only against
pre-granted standing capabilities (a standing grant derives a single-use,
content-bound capability at run time; a grant-less write is skipped and
recorded, never prompted), `gated` pauses writes for interactive approval,
and `readonly` denies every write. Approvals are persisted, inspectable,
revocable grants scoped to action type + target + scope + content hash +
expiry; silence is never approval; denials are recorded; grants are
invalidated when task scope changes. Interactive `approve()` — which can
issue a capability without a pre-existing standing grant — is called only
by the CLI; `authorize_auto` additionally requires a standing grant and
binds the exact payload bytes at issue time (#48 decision 5).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nanodot.core.config import Config
from nanodot.paths import database_path
from nanodot.ports.github_writer import WriteCapability, payload_digest

DEFAULT_REQUEST_TTL_SECONDS = 24 * 3600
# Write requests: 4 hours (#48 decision 6) — a late-approved comment on a
# fast-moving PR references a stale head SHA; the re-ask policy covers
# overnight silences.
WRITE_REQUEST_TTL_SECONDS = 4 * 3600


class WriteForbidden(PermissionError):
    """Raised when code attempts a write action under the readonly mode."""


class Mode(str, Enum):
    READONLY = "readonly"
    GATED = "gated"  # writes pause for interactive approval; the capability is the gate
    AUTO = "auto"    # standing pre-granted capabilities only; never prompts (#48 decision 5)


DEFAULT_MODE = Mode.AUTO  # #48 decision 5: pre-granted capabilities only, never prompts

READ_ACTIONS = frozenset({"read"})
# The implemented write actions (docs/design/github-writer.md §A6).
WRITE_ACTIONS = frozenset({"comment"})
# Standing (content-unbound) grants: the AUTO pre-grant marker.
STANDING_CONTENT_HASH = ""


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
    content_hash: str = ""  # the exact payload this grant authorizes


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
    content_hash: str = ""  # binds the exact payload a write may send
    content: str = ""  # the payload the user saw when approving

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  scope TEXT NOT NULL,
  task_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL,
  revoked_at REAL,
  content_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  scope TEXT NOT NULL,
  task_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  state TEXT NOT NULL,
  content_hash TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS capabilities (
  grant_id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content TEXT NOT NULL,
  issued_at REAL NOT NULL,
  used_at REAL
);
"""


def _ensure_column(connection: sqlite3.Connection, table: str, ddl: str) -> None:
    """Additive migration for pre-writer databases (fail closed: legacy
    rows keep an empty hash, which can never match a real payload)."""
    column = ddl.split()[0]
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def invalidate_task_grants(
    connection: sqlite3.Connection, task_id: str, now: float
) -> int:
    """Invalidate old-scope authorization inside the caller's transaction.

    TaskStore can exist before PermissionCenter creates its tables. Do not
    create tables or commit here: the scope change and revocation must either
    both persist or both roll back.
    """
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('grants', 'requests')"
        )
    }
    revoked = 0
    if "grants" in tables:
        revoked = connection.execute(
            "UPDATE grants SET revoked_at=? WHERE task_id=? AND revoked_at IS NULL",
            (now, task_id),
        ).rowcount
    if "requests" in tables:
        connection.execute(
            "UPDATE requests SET state='expired' WHERE task_id=? AND state='pending'",
            (task_id,),
        )
    return revoked


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
            _ensure_column(self._conn, "grants", "content_hash TEXT NOT NULL DEFAULT ''")
            _ensure_column(self._conn, "requests", "content_hash TEXT NOT NULL DEFAULT ''")
            _ensure_column(self._conn, "requests", "content TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    # -- mode ---------------------------------------------------------------

    def mode(self) -> Mode:
        # Canonical key first; "mode" remains readable for pre-rename installs.
        try:
            configured = Config().get("permission-mode")
            if configured is None:
                configured = Config().get("mode", DEFAULT_MODE.value)
            mode = Mode(str(configured))
        except ValueError:
            return Mode.READONLY  # unknown or hand-edited value: fail closed
        return mode

    def assert_allowed(self, action: str) -> None:
        """The gate every external action must pass. Readonly admits reads
        only; gated and auto additionally admit exactly the implemented
        write actions — a grant or capability is still required to execute.
        An unsupported mode denies as WriteForbidden: fail closed."""
        if action in READ_ACTIONS:
            return
        try:
            mode = self.mode()
        except ValueError as error:
            raise WriteForbidden(str(error)) from error
        if mode is not Mode.READONLY and action in WRITE_ACTIONS:
            return
        raise WriteForbidden(
            f"action {action!r} is not an available action in the current mode"
        )

    # -- requests -----------------------------------------------------------

    def request(
        self,
        action: str,
        target: str,
        scope: str,
        task_id: str,
        content: dict | None = None,
        ttl: float = DEFAULT_REQUEST_TTL_SECONDS,
    ) -> ApprovalRequest:
        """Record what action is wanted, binding the exact payload a write
        may send (canonical JSON shared with the wire). Silence never
        approves: it expires."""
        now = self._clock.time()
        stored = payload_digest(content) if content is not None else ""
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:12],
            action=action,
            target=target,
            scope=scope,
            task_id=task_id,
            created_at=now,
            expires_at=now + ttl,
            state="pending",
            content_hash=stored,
            content=json.dumps(content, sort_keys=True, separators=(",", ":")) if content is not None else "",
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    req.id, req.action, req.target, req.scope, req.task_id,
                    req.created_at, req.expires_at, req.state,
                    req.content_hash, req.content,
                ),
            )
            self._conn.commit()
        return req

    def approve(self, request_id: str) -> WriteCapability:
        """Approval creates a grant scoped to exactly this action + target +
        scope + content hash, with an expiry — and issues the single-use
        capability the writer port requires. Nothing broader."""
        with self._lock:
            # Reserve the write before checking the request, so another
            # connection cannot approve it twice or change its task's scope
            # between validation and the grant insert.
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                now = self._clock.time()
                self._expire_requests(now)
                req = self._require_request(request_id)
                if req.state != "pending":
                    # Keep expiry persisted even when returning an error.
                    self._conn.commit()
                    if req.state == "expired":
                        raise ValueError("request expired — silence is not approval; re-request")
                    raise ValueError(f"request is already {req.state}")
                grant_id = uuid.uuid4().hex[:12]
                grant = Grant(
                    id=grant_id,
                    action=req.action,
                    target=req.target,
                    scope=req.scope,
                    task_id=req.task_id,
                    created_at=now,
                    expires_at=req.expires_at,
                    revoked_at=None,
                    content_hash=req.content_hash,
                )
                self._conn.execute(
                    "UPDATE requests SET state='approved' WHERE id=?", (request_id,)
                )
                self._conn.execute(
                    "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        grant.id, grant.action, grant.target, grant.scope,
                        grant.task_id, grant.created_at, grant.expires_at,
                        None, grant.content_hash,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO capabilities VALUES (?,?,?,?,?,?,NULL)",
                    (
                        grant.id, req.action, req.target, req.content_hash,
                        req.content, now,
                    ),
                )
        return WriteCapability(
            action=req.action,
            target=req.target,
            content_hash=req.content_hash,
            grant_id=grant_id,
        )

    # -- standing grants (AUTO) -----------------------------------------------

    def create_standing_grant(
        self,
        action: str,
        target: str,
        scope: str,
        task_id: str,
        expires_at: float | None = None,
    ) -> Grant:
        """The AUTO pre-grant: authorize an action+target+scope without
        binding payload bytes (content_hash stays empty). Created only by
        an explicit CLI act (#55); scope changes revoke it like any grant."""
        now = self._clock.time()
        grant = Grant(
            id=uuid.uuid4().hex[:12],
            action=action,
            target=target,
            scope=scope,
            task_id=task_id,
            created_at=now,
            expires_at=expires_at,
            revoked_at=None,
            content_hash=STANDING_CONTENT_HASH,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    grant.id, grant.action, grant.target, grant.scope,
                    grant.task_id, grant.created_at, grant.expires_at,
                    None, grant.content_hash,
                ),
            )
            self._conn.commit()
        return grant

    def authorize_auto(
        self,
        action: str,
        target: str,
        scope: str,
        task_id: str,
        payload: dict,
    ) -> WriteCapability | None:
        """AUTO's execution gate: a standing grant derives a single-use,
        content-bound capability for THIS exact payload — the payload bytes
        are hashed at issue time, so every auto write keeps the same
        audit and replay trail as an interactive approval. Requires auto
        mode; returns None when no live standing grant matches (the caller
        skips and records, never prompts)."""
        if self.mode() is not Mode.AUTO:
            raise WriteForbidden("authorize_auto requires auto mode")
        self.assert_allowed(action)
        digest = payload_digest(payload)
        now = self._clock.time()
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            standing = self._conn.execute(
                "SELECT * FROM grants WHERE action=? AND target=? AND scope=? "
                "AND task_id=? AND content_hash=? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (action, target, scope, task_id, STANDING_CONTENT_HASH, now),
            ).fetchone()
            if standing is None:
                return None
            child_id = uuid.uuid4().hex[:12]
            content = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            )
            self._conn.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    child_id, action, target, scope, task_id,
                    now, standing["expires_at"], None, digest,
                ),
            )
            self._conn.execute(
                "INSERT INTO capabilities VALUES (?,?,?,?,?,?,NULL)",
                (child_id, action, target, digest, content, now),
            )
        return WriteCapability(
            action=action, target=target, content_hash=digest, grant_id=child_id,
        )

    # -- capabilities --------------------------------------------------------

    def consume(self, grant_id: str) -> bool:
        """Atomically mark a capability used. True exactly once per
        capability — a lost race (or a replay after restart) returns False
        and the caller must not send."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "UPDATE capabilities SET used_at=? "
                "WHERE grant_id=? AND used_at IS NULL",
                (self._clock.time(), grant_id),
            )
            return cursor.rowcount == 1

    def pending_capabilities(self, task_id: str) -> list[tuple[WriteCapability, str]]:
        """Unconsumed, unrevoked, unexpired capabilities for a task, with
        the exact approved content to send."""
        at = self._clock.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.* FROM capabilities c JOIN grants g ON c.grant_id = g.id "
                "WHERE g.task_id=? AND c.used_at IS NULL "
                "AND g.revoked_at IS NULL "
                "AND (g.expires_at IS NULL OR g.expires_at > ?) "
                "ORDER BY c.issued_at",
                (task_id, at),
            ).fetchall()
        return [
            (
                WriteCapability(
                    action=row["action"], target=row["target"],
                    content_hash=row["content_hash"], grant_id=row["grant_id"],
                ),
                json.loads(row["content"]) if row["content"] else {},
            )
            for row in rows
        ]

    def has_verbatim_request(self, task_id: str, content_hash: str) -> str | None:
        """The state of an earlier request for this exact content, if any —
        a denial is never re-asked verbatim, and a pending one is not
        duplicated."""
        states = self.verbatim_request_states(task_id, content_hash)
        return states[0][0] if states else None

    def verbatim_request_states(
        self, task_id: str, content_hash: str
    ) -> list[tuple[str, str]]:
        """Every (state, request_id) for this exact content, newest first —
        the re-ask policy reads how many times silence has expired."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, id FROM requests WHERE task_id=? AND content_hash=? "
                "ORDER BY created_at DESC",
                (task_id, content_hash),
            ).fetchall()
        return [(row["state"], row["id"]) for row in rows]

    def mark_silence_denied(self, request_id: str) -> bool:
        """The second silence is an answer: an expired request becomes a
        terminal denial (#48 decision 3), never re-asked verbatim."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE requests SET state='denied' WHERE id=? AND state='expired'",
                (request_id,),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def deny(self, request_id: str) -> None:
        """A denial is an answer: recorded, never re-asked verbatim."""
        self._transition_request(request_id, "denied")

    def sweep_expired(self) -> int:
        now = self._clock.time()
        with self._lock:
            count = self._expire_requests(now)
            self._conn.commit()
            return count

    def pending(self) -> list[ApprovalRequest]:
        return self._requests_where("state='pending'")

    def request_history(self) -> list[ApprovalRequest]:
        return self._requests_where("1=1")

    # -- grants ---------------------------------------------------------------

    def grants(self, active_only: bool = False) -> list[Grant]:
        where = (
            "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"
            if active_only else "1=1"
        )
        params = (self._clock.time(),) if active_only else ()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM grants WHERE {where} ORDER BY created_at DESC", params
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
        for grant in self.grants():
            if (
                grant.revoked_at is None
                and grant.action == action
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
            count = invalidate_task_grants(self._conn, task_id, self._clock.time())
            self._conn.commit()
            return count

    # -- internals ----------------------------------------------------------------

    def _expire_requests(self, now: float) -> int:
        return self._conn.execute(
            "UPDATE requests SET state='expired' "
            "WHERE state='pending' AND expires_at <= ?",
            (now,),
        ).rowcount

    def _transition_request(self, request_id: str, state: str) -> None:
        with self._lock:
            self._expire_requests(self._clock.time())
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
            self._expire_requests(self._clock.time())
            self._conn.commit()
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
            content_hash=row["content_hash"],
            content=row["content"],
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

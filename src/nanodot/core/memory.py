"""Memory — durable cross-task knowledge, with a confirmation-gated write
path, provenance on every item, and deletion that actually deletes.

Design (issue #12, its own track):

- Task state and the activity log are NOT memory; history never silently
  graduates into memory items.
- Exactly three write paths:
    1. user statement (CLI)                  -> confirmed
    2. evidenced terminal task outcome       -> observation, auto-recorded
       by the runner with provenance pointing at evidence
    3. model proposal                        -> proposed, requires explicit
       CLI confirmation; unconfirmed proposals expire
  There is no code path by which model output becomes confirmed memory —
  enforced by tests (an AST boundary check plus behavior tests).
- Every item carries provenance; no credentials ever (redaction at write).
- Deletion removes content from the store and therefore from anything the
  read path would inject; the activity log keeps a contentless tombstone.
- The read path is LOCAL in the MVP: memory shapes defaults and surfaces
  relevant context at task creation and summarization time on-host, but
  memory items are never attached to outbound requests (the egress
  whitelist in core.egress governs those). Extending egress to include
  user-confirmed preferences would be a deliberate, documented change.
- The task loop must run correctly with an empty memory store: memory
  accelerates, never gates.
"""

from __future__ import annotations

import json
import sqlite3
import string
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from nanodot.core.activity import ActivityLog
from nanodot.core.redaction import Redactor
from nanodot.paths import database_path

KIND_PREFERENCE = "preference"
KIND_OBSERVATION = "observation"
KIND_FACT = "fact"

STATUS_CONFIRMED = "confirmed"
STATUS_PROPOSED = "proposed"

DEFAULT_PROPOSAL_EXPIRY_SECONDS = 14 * 24 * 3600  # 14 days


def _require_content(content: str) -> None:
    """Empty items can never match anything and would persist forever."""
    if not content or not content.strip():
        raise ValueError("memory content must not be empty")


def _keywords(text: str) -> set[str]:
    """One tokenizer for both query and content sides of relevant_to.

    Recorded observations carry the PR identity as 'owner/repo#12: ...', so
    splitting on '#' and trimming edge punctuation must happen identically
    on both sides or the one token guaranteed shared — the target itself —
    can never match.
    """
    words = set()
    for token in text.replace("#", " ").split():
        trimmed = token.strip(string.punctuation)
        if len(trimmed) > 3:
            words.add(trimmed.lower())
    return words


@dataclass(frozen=True)
class MemoryItem:
    id: str
    kind: str
    content: str
    status: str
    provenance: dict
    created_at: float
    last_used: float | None = None
    expires_at: float | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  provenance TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  last_used REAL,
  expires_at REAL
);
"""


class MemoryStore:
    def __init__(
        self,
        path: Path | None = None,
        redactor: Redactor | None = None,
        activity: ActivityLog | None = None,
        clock=time,
    ) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor or Redactor(_NullSecrets())
        self._activity = activity
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # Explicitly remove deleted proposal/user content from freed
            # database pages rather than relying on SQLite build defaults.
            self._conn.execute("PRAGMA secure_delete=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.sweep_expired()

    # -- write paths --------------------------------------------------------

    def add_user(
        self, content: str, kind: str = KIND_PREFERENCE, at: float | None = None
    ) -> MemoryItem:
        """Write path 1: the user stated it. Enters confirmed."""
        _require_content(content)
        return self._insert(
            kind=kind,
            content=content,
            status=STATUS_CONFIRMED,
            provenance={"source": "user"},
            at=at,
        )

    def add_observation(
        self,
        content: str,
        task_id: str,
        evidence_ref: str,
        at: float | None = None,
    ) -> MemoryItem:
        """Write path 2: an evidenced terminal outcome, auto-recorded by
        the runner. Observation, confirmed-by-evidence, provenance links
        the evidence."""
        _require_content(content)
        return self._insert(
            kind=KIND_OBSERVATION,
            content=content,
            status=STATUS_CONFIRMED,
            provenance={"source": f"task:{task_id}", "evidence": evidence_ref},
            at=at,
        )

    def propose(
        self,
        content: str,
        kind: str = KIND_PREFERENCE,
        source: str = "model",
        at: float | None = None,
        expires_in: float = DEFAULT_PROPOSAL_EXPIRY_SECONDS,
    ) -> MemoryItem:
        """Write path 3: a proposal (e.g. from a model). Proposed only —
        confirmation is a separate, user-driven act."""
        _require_content(content)
        now = at if at is not None else self._clock.time()
        return self._insert(
            kind=kind,
            content=content,
            status=STATUS_PROPOSED,
            provenance={"source": source},
            at=now,
            expires_at=now + expires_in,
        )

    def confirm(self, item_id: str, at: float | None = None) -> MemoryItem:
        with self._lock:
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                now = at if at is not None else self._clock.time()
                row = self._conn.execute(
                    "SELECT * FROM memory WHERE id=?", (item_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"no such memory item {item_id}")
                item = self._row_to_item(row)
                if item.status != STATUS_PROPOSED:
                    raise ValueError(f"item {item_id} is not a proposal")
                expired = item.expires_at is not None and item.expires_at <= now
                if expired:
                    self._conn.execute("DELETE FROM memory WHERE id=?", (item_id,))
                else:
                    self._conn.execute(
                        "UPDATE memory SET status=?, expires_at=NULL WHERE id=?",
                        (STATUS_CONFIRMED, item_id),
                    )
        if expired:
            self._tombstone(item_id)
            raise ValueError(f"proposal {item_id} expired; propose it again to confirm")
        return replace(item, status=STATUS_CONFIRMED, expires_at=None)

    def edit(self, item_id: str, content: str) -> MemoryItem:
        _require_content(content)
        self._require(item_id)
        with self._lock:
            self._conn.execute(
                "UPDATE memory SET content=? WHERE id=?",
                (self._redactor.scrub(content), item_id),
            )
            self._conn.commit()
        return self._require(item_id)

    def remove(self, item_id: str) -> None:
        """Deletion: content gone from the store (and thus from every
        future read); activity keeps a contentless tombstone."""
        self._require(item_id)
        with self._lock:
            self._conn.execute("DELETE FROM memory WHERE id=?", (item_id,))
            self._conn.commit()
        self._tombstone(item_id)

    def sweep_expired(self, now: float | None = None) -> int:
        """Drop proposals whose confirmation window lapsed."""
        at = now if now is not None else self._clock.time()
        with self._lock:
            with self._conn:
                # Hold a write reservation across selection and deletion so
                # confirmation on another connection cannot race this sweep.
                self._conn.execute("BEGIN IMMEDIATE")
                rows = self._conn.execute(
                    "SELECT id FROM memory WHERE status=? AND expires_at IS NOT NULL "
                    "AND expires_at <= ?",
                    (STATUS_PROPOSED, at),
                ).fetchall()
                self._conn.execute(
                    "DELETE FROM memory WHERE status=? AND expires_at IS NOT NULL "
                    "AND expires_at <= ?",
                    (STATUS_PROPOSED, at),
                )
        for row in rows:
            self._tombstone(row["id"])
        return len(rows)

    # -- read paths -----------------------------------------------------------

    def get(self, item_id: str) -> MemoryItem | None:
        self.sweep_expired()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory WHERE id=?", (item_id,)
            ).fetchone()
        return self._row_to_item(row) if row else None

    def list(
        self,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[MemoryItem]:
        self.sweep_expired()
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM memory {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def count(self, status: str | None = None) -> int:
        self.sweep_expired()
        clause, params = ("WHERE status=?", [status]) if status else ("", [])
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM memory {clause}", params
            ).fetchone()
        return int(row[0])

    def context_for_prompts(self) -> list[str]:
        """Confirmed content only — what the local read path may consult."""
        return [item.content for item in self.list(status=STATUS_CONFIRMED)]

    def relevant_to(self, text: str) -> list[MemoryItem]:
        """Confirmed items whose content shares words with the text — the
        local 'remembered context' surfaced at task creation."""
        words = _keywords(text)
        out = []
        for item in self.list(status=STATUS_CONFIRMED):
            if _keywords(item.content) & words:
                out.append(item)
        return out

    def touch(self, item_ids: list[str], at: float | None = None) -> None:
        now = at if at is not None else self._clock.time()
        with self._lock:
            for item_id in item_ids:
                self._conn.execute(
                    "UPDATE memory SET last_used=? WHERE id=?", (now, item_id)
                )
            self._conn.commit()

    # -- internals ---------------------------------------------------------------

    def _insert(
        self,
        kind: str,
        content: str,
        status: str,
        provenance: dict,
        at: float | None,
        expires_at: float | None = None,
    ) -> MemoryItem:
        item = MemoryItem(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            content=self._redactor.scrub(content),
            status=status,
            provenance=dict(self._redactor.scrub_dict(provenance)),
            created_at=at if at is not None else self._clock.time(),
            expires_at=expires_at,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory VALUES (?,?,?,?,?,?,?,?)",
                (
                    item.id,
                    item.kind,
                    item.content,
                    item.status,
                    json.dumps(item.provenance),
                    item.created_at,
                    item.last_used,
                    item.expires_at,
                ),
            )
            self._conn.commit()
        return item

    def _require(self, item_id: str) -> MemoryItem:
        item = self.get(item_id)
        if item is None:
            raise ValueError(f"no such memory item {item_id}")
        return item

    def _tombstone(self, item_id: str) -> None:
        if self._activity is not None:
            self._activity.tombstone(task_id="memory", ref=f"memory item {item_id}")

    def _row_to_item(self, row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            kind=row["kind"],
            content=row["content"],
            status=row["status"],
            provenance=json.loads(row["provenance"]),
            created_at=row["created_at"],
            last_used=row["last_used"],
            expires_at=row["expires_at"],
        )

    def close(self) -> None:
        self._conn.close()


class _NullSecrets:
    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None: ...

    def unset(self, name: str) -> None: ...

    def names(self) -> list[str]:
        return []

"""Append-only activity log: what actually ran, with evidence.

Distinct from memory (durable cross-task knowledge) and from task state —
this is history. Secrets are scrubbed at write time; deletion requests
from the memory layer record contentless tombstones here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from nanodot.core.redaction import Redactor
from nanodot.paths import database_path

TOMBSTONE = "memory-deleted"


@dataclass(frozen=True)
class ActivityEntry:
    id: str
    at: float
    task_id: str
    kind: str
    message: str
    evidence: dict

    @property
    def is_tombstone(self) -> bool:
        return self.kind == TOMBSTONE


_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
  id TEXT PRIMARY KEY,
  at REAL NOT NULL,
  task_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_activity_task ON activity(task_id, at);
"""


class ActivityLog:
    def __init__(
        self,
        path: Path | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor or Redactor(_NullSecrets())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def append(
        self,
        task_id: str,
        kind: str,
        message: str,
        evidence: dict | None = None,
        at: float | None = None,
    ) -> ActivityEntry:
        entry = ActivityEntry(
            id=uuid.uuid4().hex[:12],
            at=at if at is not None else time.time(),
            task_id=task_id,
            kind=kind,
            message=self._redactor.scrub(message),
            evidence=dict(self._redactor.scrub_dict(evidence or {})),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO activity VALUES (?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.at,
                    entry.task_id,
                    entry.kind,
                    entry.message,
                    json.dumps(entry.evidence),
                ),
            )
            self._conn.commit()
        return entry

    def tombstone(self, task_id: str, ref: str, at: float | None = None) -> None:
        """Record that something was deleted — the fact of deletion, not
        the deleted content."""
        self.append(
            task_id=task_id,
            kind=TOMBSTONE,
            message=f"deleted: {self._redactor.scrub(ref)}",
            evidence={},
            at=at,
        )

    def query(
        self,
        task_id: str | None = None,
        kinds: tuple[str, ...] | None = None,
        exclude_kinds: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> list[ActivityEntry]:
        clauses, params = [], []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if exclude_kinds:
            clauses.append(f"kind NOT IN ({','.join('?' * len(exclude_kinds))})")
            params.extend(exclude_kinds)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            # rowid breaks ties by insertion order: entries written at the
            # same second replay as observe → decide → act.
            rows = self._conn.execute(
                f"SELECT * FROM activity {where} ORDER BY at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            ActivityEntry(
                id=row["id"],
                at=row["at"],
                task_id=row["task_id"],
                kind=row["kind"],
                message=row["message"],
                evidence=json.loads(row["evidence"]),
            )
            for row in rows
        ]

    def close(self) -> None:
        self._conn.close()


class _NullSecrets:
    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None: ...

    def unset(self, name: str) -> None: ...

    def names(self) -> list[str]:
        return []

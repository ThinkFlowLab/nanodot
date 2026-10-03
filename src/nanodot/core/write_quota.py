"""The write quota — the hard bound on write volume (#53, #48 decision 4).

Write pressure can never destabilize the system: each action is bounded
per watch per UTC day, charging is idempotent per exact content (the same
evidence digest never double-charges), and exhaustion is a fail-closed
degrade — the action stops being attempted for the day and the watch
loop, reads, and raw notifications continue untouched. Budgets default
to #48 decision 8 (3 comments / 3 labels / 1 approval per watch per day)
and can be overridden per watch.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from nanodot.paths import database_path

# #48 decision 8: approvals are heavier social signals than comments.
DEFAULT_WRITE_BUDGETS: dict[str, int] = {
    "comment": 3,
    "label": 3,
    "approve": 1,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_quota (
  day TEXT NOT NULL,
  task_id TEXT NOT NULL,
  action TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  charged_at REAL NOT NULL,
  PRIMARY KEY (day, task_id, action, content_hash)
);
CREATE TABLE IF NOT EXISTS write_quota_overrides (
  task_id TEXT NOT NULL,
  action TEXT NOT NULL,
  budget INTEGER NOT NULL,
  PRIMARY KEY (task_id, action)
);
"""


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


class WriteQuota:
    """Per-action, per-watch, per-day write budgets in the shared database."""

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

    def budget(self, action: str, task_id: str) -> int:
        """The effective budget: a per-watch override, else the default."""
        with self._lock:
            row = self._conn.execute(
                "SELECT budget FROM write_quota_overrides "
                "WHERE task_id=? AND action=?",
                (task_id, action),
            ).fetchone()
        if row is not None:
            return int(row["budget"])
        return DEFAULT_WRITE_BUDGETS.get(action, 0)  # unknown action: none

    def set_budget(self, task_id: str, action: str, budget: int) -> None:
        if budget < 0:
            raise ValueError("write budget must be >= 0")
        with self._lock:
            self._conn.execute(
                "INSERT INTO write_quota_overrides (task_id, action, budget) "
                "VALUES (?,?,?) ON CONFLICT(task_id, action) "
                "DO UPDATE SET budget=excluded.budget",
                (task_id, action, budget),
            )
            self._conn.commit()

    def charged_today(self, action: str, task_id: str, now: float) -> int:
        """Distinct contents charged for this action/watch so far today."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM write_quota "
                "WHERE day=? AND task_id=? AND action=?",
                (_utc_day(now), task_id, action),
            ).fetchone()
        return int(row["n"])

    def remaining(self, action: str, task_id: str, now: float) -> int:
        return max(
            0, self.budget(action, task_id) - self.charged_today(action, task_id, now)
        )

    def try_charge(
        self, action: str, task_id: str, content_hash: str, now: float
    ) -> bool:
        """Charge one issued write, at most once per exact content per day.
        True when the write is within budget (including an idempotent
        re-charge of already-charged content); False when the budget is
        exhausted — nothing was charged and nothing should be sent."""
        day = _utc_day(now)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            charged = self._conn.execute(
                "SELECT 1 FROM write_quota "
                "WHERE day=? AND task_id=? AND action=? AND content_hash=?",
                (day, task_id, action, content_hash),
            ).fetchone()
            if charged is not None:
                return True  # idempotent: the same evidence never pays twice
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM write_quota "
                "WHERE day=? AND task_id=? AND action=?",
                (day, task_id, action),
            ).fetchone()["n"]
            if count >= self.budget(action, task_id):
                return False
            self._conn.execute(
                "INSERT INTO write_quota VALUES (?,?,?,?,?)",
                (day, task_id, action, content_hash, now),
            )
            return True

    def close(self) -> None:
        self._conn.close()

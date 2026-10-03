"""The hard write budget — per action, per watch, per day (issue #53).

Pure infrastructure: counters in the shared SQLite home, default budgets,
per-watch overrides, and a fail-closed exhaustion path. A write consumes
the budget once — at issue, keyed by the evidence digest, so a retry of
the same write never double-counts. Exhaustion is an outcome, never an
error: the run degrades exactly like provider unavailability, the action
is simply not attempted again until the day rolls over.

Unknown actions fail closed (limit 0) unless an override names a limit.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from nanodot.core.activity import ActivityLog
from nanodot.paths import database_path

QUOTA_EXHAUSTED = "quota-exhausted"

# Default daily budgets per watch (issue #48, decision 4).
DEFAULT_LIMITS: dict[str, int] = {
    "comment": 3,
    "label": 3,
    "approve": 1,
}


@dataclass(frozen=True)
class QuotaDecision:
    action: str
    watch_id: str
    day: str
    limit: int
    consumed: int
    allowed: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_quota (
  action TEXT NOT NULL,
  watch_id TEXT NOT NULL,
  day TEXT NOT NULL,
  evidence_digest TEXT NOT NULL,
  consumed_at REAL NOT NULL,
  PRIMARY KEY (action, watch_id, day, evidence_digest)
);
CREATE TABLE IF NOT EXISTS write_quota_overrides (
  watch_id TEXT NOT NULL,
  action TEXT NOT NULL,
  daily_limit INTEGER NOT NULL,
  PRIMARY KEY (watch_id, action)
);
CREATE TABLE IF NOT EXISTS write_quota_exhausted (
  action TEXT NOT NULL,
  watch_id TEXT NOT NULL,
  day TEXT NOT NULL,
  notified_at REAL NOT NULL,
  PRIMARY KEY (action, watch_id, day)
);
"""


class WriteQuota:
    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        tzname: str | None = None,
        activity: ActivityLog | None = None,
        limits: dict[str, int] | None = None,
    ) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        # The budget day is the user's local day by default; injectable so
        # day-boundary tests are deterministic regardless of the host TZ.
        self._tz = ZoneInfo(tzname) if tzname else datetime.now().astimezone().tzinfo
        self._activity = activity
        self._limits = dict(limits) if limits is not None else dict(DEFAULT_LIMITS)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- configuration ------------------------------------------------------

    def set_override(self, watch_id: str, action: str, daily_limit: int) -> None:
        if type(daily_limit) is not int or daily_limit < 0:
            raise ValueError("a daily limit must be a nonnegative integer")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO write_quota_overrides VALUES (?,?,?)",
                (watch_id, action, daily_limit),
            )
            self._conn.commit()

    def clear_override(self, watch_id: str, action: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM write_quota_overrides WHERE watch_id=? AND action=?",
                (watch_id, action),
            )
            self._conn.commit()

    def limit_for(self, action: str, watch_id: str) -> int:
        # Reads take the same lock as writes: the CLI may set an override
        # while the runner is checking, and one locking discipline for the
        # shared connection is cheaper to reason about than two.
        with self._lock:
            row = self._conn.execute(
                "SELECT daily_limit FROM write_quota_overrides "
                "WHERE watch_id=? AND action=?",
                (watch_id, action),
            ).fetchone()
            if row is not None:
                return int(row["daily_limit"])
            return self._limits.get(action, 0)  # unknown action: fail closed

    # -- the budget -----------------------------------------------------------

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, self._tz).date().isoformat()

    def consumed(self, action: str, watch_id: str, now: float) -> int:
        day = self._day(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) AS n FROM write_quota "
                "WHERE action=? AND watch_id=? AND day=?",
                (action, watch_id, day),
            ).fetchone()
            return int(row["n"])

    def check(self, action: str, watch_id: str, now: float) -> QuotaDecision:
        """Read-only: may this action still be attempted right now?"""
        day = self._day(now)
        limit = self.limit_for(action, watch_id)
        used = self.consumed(action, watch_id, now)
        return QuotaDecision(
            action=action,
            watch_id=watch_id,
            day=day,
            limit=limit,
            consumed=used,
            allowed=used < limit,
        )

    def consume(
        self, action: str, watch_id: str, evidence_digest: str, now: float
    ) -> QuotaDecision:
        """Count one issued write, at most once per evidence digest.

        Denial marks exhaustion for the day (recorded once in the activity
        log) and is an outcome, never an exception — the caller degrades.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                day = self._day(now)
                limit = self.limit_for(action, watch_id)
                counted = self._conn.execute(
                    "SELECT 1 FROM write_quota "
                    "WHERE action=? AND watch_id=? AND day=? AND evidence_digest=?",
                    (action, watch_id, day, evidence_digest),
                ).fetchone()
                if counted is not None:
                    # A retry of the same write: already counted, never twice.
                    self._conn.commit()
                    used = self.consumed(action, watch_id, now)
                    return QuotaDecision(
                        action, watch_id, day, limit, used, True
                    )
                used = self.consumed(action, watch_id, now)
                if used >= limit:
                    self._conn.commit()
                    self._notify_exhausted(action, watch_id, day, now, limit)
                    return QuotaDecision(
                        action, watch_id, day, limit, used, False
                    )
                self._conn.execute(
                    "INSERT INTO write_quota VALUES (?,?,?,?,?)",
                    (action, watch_id, day, evidence_digest, now),
                )
                self._conn.commit()
                return QuotaDecision(
                    action, watch_id, day, limit, used + 1, True
                )
            except Exception:
                self._conn.rollback()
                raise

    def _notify_exhausted(
        self, action: str, watch_id: str, day: str, now: float, limit: int
    ) -> None:
        """Record the exhaustion once per action/watch/day — an outcome in
        the activity log, never an error for the run.

        Runs after the denial transaction commits: the activity log has its
        own connection, and the marker row is written only after a
        successful append, so a failed log write retries on the next
        denial instead of being silently lost.
        """
        if self._activity is None:
            self._conn.execute(
                "INSERT OR IGNORE INTO write_quota_exhausted VALUES (?,?,?,?)",
                (action, watch_id, day, now),
            )
            self._conn.commit()
            return
        seen = self._conn.execute(
            "SELECT 1 FROM write_quota_exhausted "
            "WHERE action=? AND watch_id=? AND day=?",
            (action, watch_id, day),
        ).fetchone()
        if seen is not None:
            return
        try:
            self._activity.append(
                task_id=watch_id,
                kind=QUOTA_EXHAUSTED,
                message=(
                    f"{action} budget exhausted for {day} "
                    f"({limit}/day); not attempting further {action} "
                    "writes today"
                ),
                at=now,
            )
        except Exception:  # noqa: BLE001 — the budget never fails the run
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO write_quota_exhausted VALUES (?,?,?,?)",
            (action, watch_id, day, now),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

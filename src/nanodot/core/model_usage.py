"""Per-provider model usage counters and the opt-in daily call budget.

Purely local accounting: every provider response's token usage
accumulates into the shared SQLite home, and `model-daily-limit` (if
set) caps calls per provider per local day. The budget fails closed on
the same path as any provider unavailability — ProviderError — so the
watch degrades and raw notifications are unaffected. No limit configured
means no limit enforced.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from nanodot.paths import database_path

DAILY_LIMIT_KEY = "model-daily-limit"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_usage (
  provider TEXT NOT NULL,
  day TEXT NOT NULL,
  calls INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  PRIMARY KEY (provider, day)
);
"""


class ModelUsage:
    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        tzname: str | None = None,
    ) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._tz = ZoneInfo(tzname) if tzname else datetime.now().astimezone().tzinfo
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _day(self, now: float | None = None) -> str:
        at = self._clock() if now is None else now
        return datetime.fromtimestamp(at, self._tz).date().isoformat()

    def record(
        self,
        provider: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
        now: float | None = None,
    ) -> None:
        day = self._day(now)
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_usage (provider, day, calls, input_tokens, "
                "output_tokens) VALUES (?,?,?,?,?) "
                "ON CONFLICT(provider, day) DO UPDATE SET "
                "calls = calls + excluded.calls, "
                "input_tokens = input_tokens + excluded.input_tokens, "
                "output_tokens = output_tokens + excluded.output_tokens",
                (provider, day, calls, input_tokens, output_tokens),
            )
            self._conn.commit()

    def calls_today(self, provider: str, now: float | None = None) -> int:
        row = self._conn.execute(
            "SELECT calls FROM model_usage WHERE provider=? AND day=?",
            (provider, self._day(now)),
        ).fetchone()
        return int(row["calls"]) if row else 0

    def summary(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT provider, day, calls, input_tokens, output_tokens "
            "FROM model_usage ORDER BY day DESC, provider LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

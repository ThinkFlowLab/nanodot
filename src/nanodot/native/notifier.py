"""Native notification sink: a deduplicated persisted inbox plus a macOS
notification for notable/terminal events.

Channel follows the proposed default from issue #1 (macOS notification +
inbox), pending confirmation in review. Dedup is by event content key and
survives restarts, so a runner replaying after downtime notifies once.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from nanodot.core.redaction import Redactor
from nanodot.core.statemachine import WatchEvent
from nanodot.paths import database_path

DEFAULT_TITLE = "nanodot"


def _default_osascript(*args: object, **kwargs: object) -> None:
    subprocess.run(*args, **kwargs)  # type: ignore[arg-type]


def event_key(event: WatchEvent) -> str:
    """Stable identity of an event: same content, same key, one delivery."""
    payload = json.dumps(
        {
            "task_id": event.task_id,
            "kind": event.kind,
            "message": event.message,
            "evidence": event.evidence,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class InboxEntry:
    id: str
    at: float
    task_id: str
    kind: str
    message: str
    evidence: dict


_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
  id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  at REAL NOT NULL,
  task_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}'
);
"""


class NativeNotifier:
    """Inbox-first sink; the OS notification is best-effort on top."""

    def __init__(
        self,
        path: Path | None = None,
        redactor: Redactor | None = None,
        os_notify: bool = True,
        osascript_runner: Callable[..., None] | None = None,
    ) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor or Redactor(_NullSecrets())
        self._os_notify_enabled = os_notify
        self._osascript = osascript_runner or _default_osascript
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.os_notifications: list[str] = []  # record of delivered texts

    def notify(self, event: WatchEvent) -> None:
        if not event.notable:  # intermediate polls never notify (belt)
            return
        message = self._redactor.scrub(event.message)
        key = event_key(event)
        with self._lock:
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?)",
                (
                    f"{key[:12]}-{event.at:.0f}",
                    key,
                    event.at or time.time(),
                    event.task_id,
                    event.kind,
                    message,
                    json.dumps(self._redactor.scrub_dict(event.evidence)),
                ),
            ).rowcount
            self._conn.commit()
        if inserted:
            self._deliver(f"{event.kind}: {message}")

    def _deliver(self, text: str) -> None:
        self.os_notifications.append(text)
        if not self._os_notify_enabled:
            return
        try:
            self._osascript(
                [
                    "osascript",
                    "-e",
                    f'display notification "{text}" with title "{DEFAULT_TITLE}"',
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:  # noqa: BLE001 — notifications are best-effort
            pass

    def list(self, limit: int = 50) -> list[InboxEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM inbox ORDER BY at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            InboxEntry(
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

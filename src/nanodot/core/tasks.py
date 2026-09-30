"""Task model and persistent task store — the heart of the task inbox.

A task records its exact scope at creation (target, purpose, cadence,
allowed actions, notification and stop conditions) so a watch can never
drift beyond it. Changing scope is an explicit operation that bumps
``scope_version``, which the permission layer uses to invalidate grants.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from nanodot.core.redaction import Redactor
from nanodot.paths import database_path

READ_ONLY_ACTIONS = frozenset({"read"})

# The MVP implements one fixed watch policy, not a natural-language rule
# interpreter. Keep the two previously shipped default spellings as explicit
# aliases so existing default watches remain usable without rewriting scope.
DEFAULT_NOTIFICATION_CONDITIONS = (
    "notify on check failures, new commits, access blockers, and terminal outcomes"
)
DEFAULT_STOP_CONDITIONS = (
    "stop when required checks pass on the current head SHA, or the PR "
    "is merged or closed"
)
SUPPORTED_NOTIFICATION_CONDITIONS = frozenset(
    {
        DEFAULT_NOTIFICATION_CONDITIONS,
        "notify on check failures and terminal outcomes",
        "check failures and terminal outcomes",
    }
)
SUPPORTED_STOP_CONDITIONS = frozenset(
    {
        DEFAULT_STOP_CONDITIONS,
        "required checks pass on the current head SHA, or the PR merges or closes",
    }
)


class TaskError(ValueError):
    """Invalid task definition or transition."""


class TaskState(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    BLOCKED = "blocked"

    @property
    def terminal(self) -> bool:
        return self in (TaskState.CANCELLED, TaskState.COMPLETED)


_TARGET_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")


@dataclass(frozen=True)
class PRTarget:
    owner: str
    repo: str
    number: int

    @classmethod
    def parse(cls, text: str) -> "PRTarget":
        match = _TARGET_RE.match(text.strip())
        if not match:
            raise TaskError(f"invalid PR target {text!r}; expected owner/repo#123")
        return cls(match.group(1), match.group(2), int(match.group(3)))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass
class Task:
    target: PRTarget
    purpose: str
    cadence_seconds: int = 300
    allowed_actions: tuple[str, ...] = ("read",)
    notification_conditions: str = DEFAULT_NOTIFICATION_CONDITIONS
    stop_conditions: str = DEFAULT_STOP_CONDITIONS
    state: TaskState = TaskState.ACTIVE
    blocker: str | None = None
    scope_version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    next_check_at: float | None = None
    watch_state: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def validate(self) -> None:
        if not self.purpose.strip():
            raise TaskError("task purpose must not be empty")
        if self.cadence_seconds <= 0:
            raise TaskError("cadence must be positive")
        extra = set(self.allowed_actions) - READ_ONLY_ACTIONS
        if extra:
            raise TaskError(
                f"actions not allowed in the read-only MVP: {sorted(extra)}"
            )
        if "read" not in self.allowed_actions:
            raise TaskError("a PR watch requires the read action")
        if self.notification_conditions not in SUPPORTED_NOTIFICATION_CONDITIONS:
            raise TaskError(
                "unsupported notification conditions; the MVP only supports: "
                + DEFAULT_NOTIFICATION_CONDITIONS
            )
        if self.stop_conditions not in SUPPORTED_STOP_CONDITIONS:
            raise TaskError(
                "unsupported stop conditions; the MVP only supports: "
                + DEFAULT_STOP_CONDITIONS
            )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  target TEXT NOT NULL,
  purpose TEXT NOT NULL,
  cadence_seconds INTEGER NOT NULL,
  allowed_actions TEXT NOT NULL,
  notification_conditions TEXT NOT NULL,
  stop_conditions TEXT NOT NULL,
  state TEXT NOT NULL,
  blocker TEXT,
  scope_version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  next_check_at REAL,
  watch_state TEXT NOT NULL DEFAULT '{}'
);
"""


class TaskStore:
    """SQLite-backed task store. Inspectable: `sqlite3 ~/.nanodot/nanodot.db`."""

    def __init__(
        self,
        path: Path | None = None,
        redactor: Redactor | None = None,
        clock: type(time) | None = None,
    ) -> None:
        self._path = path or database_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor or Redactor(_NullSecrets())
        self._time = clock or time
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- mapping helpers -------------------------------------------------

    def _scrub(self, text: str) -> str:
        return self._redactor.scrub(text)

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        # Preserve unsupported legacy scope for inspection and cancellation.
        # Loading is not authorization to execute: every runnable boundary
        # validates, and the scheduler quarantines invalid active tasks.
        return Task(
            id=row["id"],
            target=PRTarget.parse(row["target"]),
            purpose=row["purpose"],
            cadence_seconds=row["cadence_seconds"],
            allowed_actions=tuple(json.loads(row["allowed_actions"])),
            notification_conditions=row["notification_conditions"],
            stop_conditions=row["stop_conditions"],
            state=TaskState(row["state"]),
            blocker=row["blocker"],
            scope_version=row["scope_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            next_check_at=row["next_check_at"],
            watch_state=json.loads(row["watch_state"]),
        )

    def _task_to_values(self, task: Task) -> tuple:
        return (
            task.id,
            str(task.target),
            self._scrub(task.purpose),
            task.cadence_seconds,
            json.dumps(list(task.allowed_actions)),
            self._scrub(task.notification_conditions),
            self._scrub(task.stop_conditions),
            task.state.value,
            self._scrub(task.blocker) if task.blocker else None,
            task.scope_version,
            task.created_at,
            task.updated_at,
            task.next_check_at,
            json.dumps(task.watch_state),
        )

    # -- CRUD ------------------------------------------------------------

    def validate(self, task: Task) -> None:
        """Validate scope and identifiers before persistence or execution."""
        task.validate()
        if self._redactor.contains_secret(str(task.target)):
            raise TaskError("PR target contains a configured secret; use a non-secret identifier")

    def create(self, task: Task) -> Task:
        self.validate(task)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._task_to_values(task),
                )
        except sqlite3.IntegrityError as error:
            raise TaskError(f"task {task.id} already exists or has invalid fields") from error
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at"
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_schedulable(self, now: float) -> list[Task]:
        """Active tasks whose next check is due. Terminal and paused tasks
        are never returned."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE state = ? AND next_check_at <= ? "
                "ORDER BY next_check_at",
                (TaskState.ACTIVE.value, now),
            ).fetchall()
        tasks = []
        for row in rows:
            task = self._row_to_task(row)
            try:
                self.validate(task)
            except TaskError as error:
                self.set_blocked(task.id, f"invalid saved task scope: {error}")
                continue
            tasks.append(task)
        return tasks

    def update(self, task: Task) -> Task:
        self.validate(task)
        task.updated_at = self._time.time()
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._require(task.id)
            self._check_transition(current, task.state)
            if current.state is not TaskState.ACTIVE and task.state is not current.state:
                raise TaskError("task is no longer active; use an explicit lifecycle operation")
            if task.scope_version < current.scope_version:
                raise TaskError("task scope changed; reload it before updating")
            scope_fields = (
                "target", "purpose", "cadence_seconds", "allowed_actions",
                "notification_conditions", "stop_conditions",
            )
            scope_changed = any(
                getattr(task, name) != getattr(current, name) for name in scope_fields
            ) or task.scope_version > current.scope_version
            if scope_changed:
                task.scope_version = current.scope_version + 1
            else:
                task.scope_version = current.scope_version
            self._conn.execute(
                "UPDATE tasks SET target=?, purpose=?, cadence_seconds=?, "
                "allowed_actions=?, notification_conditions=?, stop_conditions=?, "
                "state=?, blocker=?, scope_version=?, created_at=?, updated_at=?, "
                "next_check_at=?, watch_state=? WHERE id=?",
                self._task_to_values(task)[1:] + (task.id,),
            )
        return task

    # -- lifecycle ---------------------------------------------------------

    def _set_state(
        self, task: Task, state: TaskState, blocker: str | None,
        now: float | None = None,
    ) -> Task:
        # State-only transitions do not rewrite scope. This deliberately lets
        # the user pause/cancel an unsupported legacy watch without approving
        # a replacement scope merely to make it stop.
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            task = self._require(task.id)
            self._check_transition(task, state)
            if state is TaskState.ACTIVE:
                self.validate(task)
                if task.state is not TaskState.ACTIVE or task.next_check_at is None:
                    task.next_check_at = now if now is not None else self._time.time()
            else:
                task.next_check_at = None
            task.state = state
            task.blocker = blocker
            task.updated_at = self._time.time()
            self._conn.execute(
                "UPDATE tasks SET state=?, blocker=?, next_check_at=?, updated_at=? "
                "WHERE id=?",
                (
                    state.value, self._scrub(blocker) if blocker else None,
                    task.next_check_at, task.updated_at, task.id,
                ),
            )
        return task

    @staticmethod
    def _check_transition(task: Task, state: TaskState) -> None:
        if task.state.terminal and state is not task.state:
            raise TaskError(f"cannot change a {task.state.value} task; create a new watch")
        if task.watch_state.get("terminal") and not state.terminal:
            raise TaskError("cannot reactivate a terminal watch; create a new watch")

    def pause(self, task_id: str) -> Task:
        return self._set_state(self._require(task_id), TaskState.PAUSED, None)

    def resume(self, task_id: str, now: float | None = None) -> Task:
        return self._set_state(self._require(task_id), TaskState.ACTIVE, None, now=now)

    def cancel(self, task_id: str) -> Task:
        return self._set_state(self._require(task_id), TaskState.CANCELLED, None)

    def complete(self, task_id: str) -> Task:
        return self._set_state(self._require(task_id), TaskState.COMPLETED, None)

    def set_blocked(self, task_id: str, reason: str) -> Task:
        return self._set_state(
            self._require(task_id), TaskState.BLOCKED, self._scrub(reason)
        )

    def flag(self, task_id: str, reason: str) -> Task:
        """Set a visible blocker while the task stays active and retrying
        (e.g. prolonged fetch failures — visible, not stopped)."""
        task = self._require(task_id)
        task.blocker = self._scrub(reason)
        return self.update(task)

    def clear_flag(self, task_id: str) -> Task:
        task = self._require(task_id)
        task.blocker = None
        return self.update(task)

    def update_scope(
        self,
        task_id: str,
        *,
        purpose: str | None = None,
        notification_conditions: str | None = None,
        stop_conditions: str | None = None,
        cadence_seconds: int | None = None,
    ) -> Task:
        """Explicit scope change: invalidates grants (permissions layer
        watches scope_version)."""
        task = self._require(task_id)
        changed = replace(
            task,
            purpose=purpose if purpose is not None else task.purpose,
            notification_conditions=(
                notification_conditions
                if notification_conditions is not None
                else task.notification_conditions
            ),
            stop_conditions=(
                stop_conditions if stop_conditions is not None else task.stop_conditions
            ),
            cadence_seconds=(
                cadence_seconds if cadence_seconds is not None else task.cadence_seconds
            ),
        )
        changed.scope_version = task.scope_version + 1
        return self.update(changed)

    def _require(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise TaskError(f"no such task {task_id}")
        return task

    def close(self) -> None:
        self._conn.close()


class _NullSecrets:
    """No secrets configured — redaction is a no-op until wired."""

    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None: ...

    def unset(self, name: str) -> None: ...

    def names(self) -> list[str]:
        return []

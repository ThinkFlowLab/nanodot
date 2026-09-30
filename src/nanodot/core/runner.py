"""The task loop — one bounded iteration of fetch → decide → record.

Core logic only: the loop depends on the SnapshotFetcher and
NotificationSink ports, never on their native implementations. The
scheduler/executor port (see adapter-seam.md) drives `run_once`.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from enum import Enum

from nanodot.core import statemachine
from nanodot.core.activity import ActivityLog
from nanodot.core.statemachine import WatchEvent
from nanodot.core.tasks import Task, TaskStore
from nanodot.ports.github import (
    AuthLostError,
    FetchError,
    PRNotFoundError,
    RetryableError,
    SnapshotFetcher,
)
from nanodot.ports.inference import InferenceProvider, ProviderError, StateChange
from nanodot.ports.notifier import NotificationSink

MAX_BACKOFF_SECONDS = 3600
PROLONGED_FAILURE_SECONDS = 3600


def state_change_from_event(event: WatchEvent) -> StateChange:
    """The whitelisted evidence for a summary, from an event."""
    checks = tuple(
        (check.get("name", ""), check.get("conclusion"))
        for check in event.evidence.get("checks", [])
    )
    return StateChange(
        kind=event.kind,
        summary=event.message,
        head_sha=event.evidence.get("head_sha", ""),
        pr_state=event.evidence.get("pr_state", ""),
        url=event.evidence.get("url", ""),
        checks=checks,
    )


def safe_summarize(provider: InferenceProvider, event: WatchEvent) -> str | None:
    """Summarize through the provider; degrade to None on any failure.
    The summary decorates the notification — the raw message is the truth."""
    try:
        return provider.summarize(state_change_from_event(event))
    except ProviderError:
        return None


class RunOutcome(str, Enum):
    OK = "ok"
    TERMINAL = "terminal"
    RETRY_SCHEDULED = "retry-scheduled"
    BLOCKED = "blocked"
    SKIPPED_TERMINAL = "skipped-terminal"
    SKIPPED_OVERLAP = "skipped-overlap"


def backoff_seconds(cadence_seconds: int, consecutive_failures: int) -> int:
    """Exponential backoff x2 per failure, capped at one hour."""
    return min(cadence_seconds * (2 ** consecutive_failures), MAX_BACKOFF_SECONDS)


class TaskLoop:
    def __init__(
        self,
        store: TaskStore,
        fetcher: SnapshotFetcher,
        sink: NotificationSink,
        activity: ActivityLog,
        provider: InferenceProvider | None = None,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._sink = sink
        self._activity = activity
        self._provider = provider
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def run_once(self, task: Task, now: float) -> RunOutcome:
        """One bounded check for one task. Safe to call concurrently:
        a second run of the same task is skipped, never overlapped."""
        if task.state.terminal or task.watch_state.get("terminal"):
            return RunOutcome.SKIPPED_TERMINAL

        lock = self._lock_for(task.id)
        if not lock.acquire(blocking=False):
            return RunOutcome.SKIPPED_OVERLAP
        try:
            return self._run_locked(task, now)
        finally:
            lock.release()

    # -- internals ---------------------------------------------------------

    def _run_locked(self, task: Task, now: float) -> RunOutcome:
        try:
            snapshot = self._fetcher.fetch(task.target)
        except (AuthLostError, PRNotFoundError) as error:
            self._block(task, error, now)
            return RunOutcome.BLOCKED
        except RetryableError as error:
            return self._schedule_retry(task, error, now)
        except FetchError as error:  # unexpected — treat as retryable
            return self._schedule_retry(task, error, now)

        failures = int(task.watch_state.get("consecutive_failures", 0))
        task.watch_state = dict(
            task.watch_state, consecutive_failures=0, last_success_at=now
        )
        if failures and task.blocker:
            task.blocker = None  # keep the in-memory task consistent too
            self._store.clear_flag(task.id)

        watch_state, events = statemachine.step(task, snapshot, now)
        task.watch_state = watch_state

        for event in events:
            self._activity.append(
                task_id=task.id,
                kind=event.kind,
                message=event.message,
                evidence=event.evidence,
                at=event.at,
            )
            if event.notable:
                if self._provider is not None:
                    summary = safe_summarize(self._provider, event)
                    if summary:
                        event = replace(event, summary=summary)
                self._sink.notify(event)

        terminal = any(event.terminal for event in events)
        if terminal:
            task.next_check_at = None
            self._store.update(task)
            self._store.complete(task.id)
            return RunOutcome.TERMINAL

        task.next_check_at = now + task.cadence_seconds
        self._store.update(task)
        return RunOutcome.OK

    def _block(self, task: Task, error: Exception, now: float) -> None:
        reason = f"blocked: {error}"
        event = WatchEvent(
            kind=statemachine.BLOCKED,
            message=reason,
            evidence={},
            notable=True,
            task_id=task.id,
            at=now,
        )
        self._activity.append(
            task_id=task.id, kind=event.kind, message=reason, at=now
        )
        self._sink.notify(event)
        task.next_check_at = None
        self._store.update(task)
        self._store.set_blocked(task.id, reason)

    def _schedule_retry(self, task: Task, error: Exception, now: float) -> RunOutcome:
        failures = int(task.watch_state.get("consecutive_failures", 0)) + 1
        watch_state = dict(task.watch_state)
        watch_state["consecutive_failures"] = failures
        if "last_success_at" not in watch_state:
            watch_state["last_success_at"] = now  # first run: start the clock
        task.watch_state = watch_state

        task.next_check_at = now + backoff_seconds(task.cadence_seconds, failures)
        self._store.update(task)

        self._activity.append(
            task_id=task.id,
            kind="fetch-retry",
            message=f"retryable fetch failure ({failures}): {error}",
            at=now,
        )

        without_success = now - float(watch_state["last_success_at"])
        if without_success >= PROLONGED_FAILURE_SECONDS and not task.blocker:
            self._store.flag(
                task.id,
                f"prolonged fetch failure: no successful check for "
                f"{int(without_success)}s ({failures} attempts)",
            )
        return RunOutcome.RETRY_SCHEDULED

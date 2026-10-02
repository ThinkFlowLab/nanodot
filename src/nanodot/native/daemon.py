"""The native scheduler/executor: a long-lived polling daemon.

Restart recovery is reconciliation, not replay: on start it simply runs
whatever is due. The state machine dedups unchanged snapshots and the
notification sink dedups by event key, so a restart after downtime
produces current state — never a flood of missed notifications.
"""

from __future__ import annotations

import logging
import threading
import time

from nanodot.core.runner import TaskLoop, backoff_seconds
from nanodot.core.tasks import Task, TaskState, TaskStore

logger = logging.getLogger(__name__)


class RunnerDaemon:
    def __init__(
        self,
        loop: TaskLoop,
        store: TaskStore,
        tick_seconds: float = 1.0,
        clock=time,
    ) -> None:
        self._loop = loop
        self._store = store
        self._tick = tick_seconds
        self._clock = clock

    def tick(self, stop: threading.Event | None = None) -> int:
        """Run due tasks once, checking for shutdown before each new task.

        An in-flight task finishes and persists its progress. Returns the
        number of tasks attempted; omitting ``stop`` runs the full pass.
        """
        now = self._clock.time()
        attempted = 0
        try:
            schedulable = self._store.list_schedulable(now)
        except Exception:
            # Store iteration runs outside the per-task guard below. A
            # transient failure (secret rotation race, momentary lock) must
            # cost this pass, not the daemon; the next tick retries. Never
            # log exception text: it can contain credentials or private data.
            logger.warning("task listing failed; will retry next pass")
            return 0
        for task in schedulable:
            if stop is not None and stop.is_set():
                break
            attempted += 1
            try:
                self._loop.run_once(task, now)
            except Exception:
                # Adapter bugs and optional-service failures must not stop
                # unrelated watches. Never log exception text: remote payloads
                # and provider errors can contain credentials or private data.
                logger.warning("task %s raised an unexpected error", task.id)
                self._retry_failed_task(task, now)
        return attempted

    def _retry_failed_task(self, task: Task, now: float) -> None:
        try:
            # run_once may have mutated its input before failing. Retry from
            # committed state, not a half-finished (possibly terminal) state.
            current = self._store.get(task.id)
            if current is None or current.state is not TaskState.ACTIVE:
                return
            try:
                failures = max(
                    0, int(current.watch_state.get("consecutive_failures", 0))
                ) + 1
            except (TypeError, ValueError, OverflowError):
                failures = 1
            current.watch_state = dict(
                current.watch_state,
                consecutive_failures=failures,
                last_success_at=current.watch_state.get("last_success_at", now),
            )
            current.next_check_at = now + backoff_seconds(
                current.cadence_seconds, min(failures, 12)
            )
            current.blocker = "unexpected task failure; retry scheduled"
            self._store.update(current)
        except Exception:
            # Even a damaged store/error-reporting path must not prevent the
            # remaining tasks in this scheduler pass from being attempted.
            logger.error("could not persist failure for task %s", task.id)

    def serve(self, stop: threading.Event, poll_seconds: float | None = None) -> None:
        """Foreground loop; stop by setting the event."""
        interval = poll_seconds if poll_seconds is not None else self._tick
        while not stop.is_set():
            self.tick(stop=stop)
            stop.wait(interval)

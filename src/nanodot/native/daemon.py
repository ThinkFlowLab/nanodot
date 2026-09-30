"""The native scheduler/executor: a long-lived polling daemon.

Restart recovery is reconciliation, not replay: on start it simply runs
whatever is due. The state machine dedups unchanged snapshots and the
notification sink dedups by event key, so a restart after downtime
produces current state — never a flood of missed notifications.
"""

from __future__ import annotations

import threading
import time

from nanodot.core.runner import TaskLoop
from nanodot.core.tasks import TaskStore


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

    def tick(self) -> int:
        """One scheduler pass: run every due task once. Returns the number
        of tasks attempted."""
        now = self._clock.time()
        attempted = 0
        for task in self._store.list_schedulable(now):
            self._loop.run_once(task, now)
            attempted += 1
        return attempted

    def serve(self, stop: threading.Event, poll_seconds: float | None = None) -> None:
        """Foreground loop; stop by setting the event."""
        interval = poll_seconds if poll_seconds is not None else self._tick
        while not stop.is_set():
            self.tick()
            stop.wait(interval)

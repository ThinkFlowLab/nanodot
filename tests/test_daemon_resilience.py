"""An unexpected failure in one watch must not stop the scheduler."""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FAILURE, SUCCESS, FakeClock, FakeGitHub, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskState, TaskStore
from nanodot.native.daemon import RunnerDaemon


@pytest.mark.parametrize("failing_component", ["fetcher", "sink"])
def test_unexpected_adapter_failure_isolated_and_retried(
    home: Path, caplog: pytest.LogCaptureFixture, failing_component: str
) -> None:
    clock = FakeClock()
    store = TaskStore(clock=clock)
    activity = ActivityLog()
    failed = store.create(
        Task(target=PRTarget.parse("o/r#1"), purpose="watch", next_check_at=0)
    )
    healthy = store.create(
        Task(target=PRTarget.parse("o/r#2"), purpose="watch", next_check_at=1)
    )
    github = {}
    for task, conclusion in ((failed, SUCCESS), (healthy, FAILURE)):
        fake = FakeGitHub(task.target)
        fake.add_check("ci", conclusion, sha=fake.head_sha)
        github[str(task.target)] = fake
    should_fail = True
    secret_error = "private-payload-with-credential"

    class Fetcher:
        def fetch(self, target):
            if should_fail and failing_component == "fetcher" and target == failed.target:
                raise KeyError(secret_error)
            return github[str(target)].fetch(target)

    class Sink(FakeSink):
        def notify(self, event):
            if should_fail and failing_component == "sink" and event.task_id == failed.id:
                raise RuntimeError(secret_error)
            super().notify(event)

    sink = Sink()
    daemon = RunnerDaemon(TaskLoop(store, Fetcher(), sink, activity), store, clock=clock)
    assert daemon.tick() == 2
    retry = store.get(failed.id)
    assert retry.state is TaskState.ACTIVE
    assert not retry.watch_state.get("terminal")
    assert retry.next_check_at == clock.time() + 600
    assert retry.watch_state["consecutive_failures"] == 1
    assert "retry scheduled" in retry.blocker
    assert store.get(healthy.id).next_check_at == clock.time() + 300
    assert [event.task_id for event in sink.events] == [healthy.id]
    assert secret_error not in caplog.text
    assert secret_error.encode() not in (home / "nanodot.db").read_bytes()

    # Retry is bounded by cadence/backoff, and a recovered terminal result
    # can finish because no partially mutated terminal state was persisted.
    assert daemon.tick() == 0
    should_fail = False
    clock.advance(600)
    assert daemon.tick() == 2
    assert store.get(failed.id).state is TaskState.COMPLETED
    assert store.get(failed.id).blocker is None
    assert [event.task_id for event in sink.events] == [healthy.id, failed.id]


def test_error_reporting_failure_does_not_stop_remaining_tasks(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    store = TaskStore(clock=clock)
    failed = store.create(
        Task(target=PRTarget.parse("o/r#1"), purpose="watch", next_check_at=0)
    )
    healthy = store.create(
        Task(target=PRTarget.parse("o/r#2"), purpose="watch", next_check_at=1)
    )
    visited = []

    class Loop:
        def run_once(self, task, now):
            visited.append(task.id)
            if task.id == failed.id:
                raise RuntimeError("adapter failure")

    def failed_update(task):
        raise RuntimeError("error reporting unavailable")

    monkeypatch.setattr(store, "update", failed_update)
    daemon = RunnerDaemon(Loop(), store, clock=clock)
    assert daemon.tick() == 2
    assert visited == [failed.id, healthy.id]


def test_daemon_does_not_swallow_process_interrupts(home: Path) -> None:
    store = TaskStore()
    store.create(Task(target=PRTarget.parse("o/r#1"), purpose="watch", next_check_at=0))

    class Loop:
        def run_once(self, task, now):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        RunnerDaemon(Loop(), store, clock=FakeClock()).tick()


def test_failure_after_completion_cannot_reactivate_task(home: Path) -> None:
    clock = FakeClock()
    store = TaskStore(clock=clock)
    task = store.create(
        Task(target=PRTarget.parse("o/r#1"), purpose="watch", next_check_at=0)
    )

    class Loop:
        def run_once(self, task, now):
            store.complete(task.id)
            raise RuntimeError("optional post-completion failure")

    daemon = RunnerDaemon(Loop(), store, clock=clock)
    assert daemon.tick() == 1
    assert store.get(task.id).state is TaskState.COMPLETED
    assert store.get(task.id).next_check_at is None
    assert daemon.tick() == 0

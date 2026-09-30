"""Native runner acceptance tests (issue #8) — fake clock throughout."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeSink, SUCCESS, TYPICAL_ERRORS

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import RunOutcome, TaskLoop, backoff_seconds
from nanodot.core.statemachine import BLOCKED, CHECKS_FAILED, CHECKS_PASSED
from nanodot.core.tasks import PRTarget, Task, TaskState, TaskStore
from nanodot.native.daemon import RunnerDaemon

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")
CADENCE = 300


class Harness:
    """Everything a tick needs, on fakes, against the real SQLite stores."""

    def __init__(self, home: Path, cadence: int = CADENCE):
        self.store = TaskStore(path=home / "nanodot.db")
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.sink = FakeSink()
        self.clock = FakeClock()
        self.loop = TaskLoop(self.store, self.github, self.sink, self.activity)
        self.task = self.store.create(
            Task(target=TARGET, purpose="watch", cadence_seconds=cadence, next_check_at=0.0)
        )

    def tick(self) -> RunOutcome:
        task = self.store.get(self.task.id)
        return self.loop.run_once(task, self.clock.now)


def test_default_cadence_and_reschedulable(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", None, sha="s1", status="queued")
    assert h.tick() is RunOutcome.OK
    task = h.store.get(h.task.id)
    assert task.next_check_at == h.clock.now + CADENCE

    # Not schedulable before the cadence elapses; schedulable after.
    assert h.store.list_schedulable(now=h.clock.now + CADENCE - 1) == []
    due = h.store.list_schedulable(now=h.clock.now + CADENCE)
    assert [t.id for t in due] == [h.task.id]


def test_per_task_cadence_override(home: Path) -> None:
    h = Harness(home, cadence=120)
    h.github.add_check("ci", None, sha="s1", status="queued")
    h.tick()
    task = h.store.get(h.task.id)
    assert task.next_check_at == h.clock.now + 120


def test_backoff_sequence_and_cap(home: Path) -> None:
    assert backoff_seconds(300, 1) == 600
    assert backoff_seconds(300, 2) == 1200
    assert backoff_seconds(300, 5) == 3600  # cap
    assert backoff_seconds(7200, 1) == 3600  # cap below cadence

    h = Harness(home)
    h.github.fail_with(TYPICAL_ERRORS["rate-limit"])
    assert h.tick() is RunOutcome.RETRY_SCHEDULED
    assert h.store.get(h.task.id).next_check_at == h.clock.now + 600

    h.clock.advance(600)
    assert h.tick() is RunOutcome.RETRY_SCHEDULED
    assert h.store.get(h.task.id).next_check_at == h.clock.now + 1200

    h.clock.advance(1200)
    assert h.tick() is RunOutcome.RETRY_SCHEDULED
    assert h.store.get(h.task.id).next_check_at == h.clock.now + 2400


def test_prolonged_failure_is_visible_but_still_retrying(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", None, sha="s1", status="queued")
    h.tick()  # success establishes last_success_at
    h.github.fail_with(TYPICAL_ERRORS["rate-limit"])
    h.clock.advance(3600)
    h.tick()
    task = h.store.get(h.task.id)
    assert task.blocker and "prolonged" in task.blocker
    assert task.state is TaskState.ACTIVE  # visible, not stopped
    # Still retrying: the retry is scheduled.
    assert task.next_check_at is not None
    # Recovery clears the flag.
    h.github.fail_with(None)
    h.clock.advance(3600)
    h.tick()
    assert h.store.get(h.task.id).blocker is None


def test_auth_loss_blocks_and_requires_user_action(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", None, sha="s1", status="queued")
    h.tick()
    h.github.fail_with(TYPICAL_ERRORS["auth"])
    assert h.tick() is RunOutcome.BLOCKED

    task = h.store.get(h.task.id)
    assert task.state is TaskState.BLOCKED
    assert "token" in task.blocker.lower() or "authorization" in task.blocker.lower()
    assert BLOCKED in h.sink.kinds()
    assert h.store.list_schedulable(now=h.clock.now + 10_000) == []

    # User fixes the token and resumes: the watch continues.
    h.github.fail_with(None)
    h.store.resume(task.id)
    task = h.store.get(h.task.id)
    task.next_check_at = h.clock.now
    h.store.update(task)
    assert h.tick() is RunOutcome.OK


def test_no_overlapping_runs_of_same_task(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", None, sha="s1", status="queued")
    lock = h.loop._lock_for(h.task.id)
    with lock:  # simulate an in-flight run
        task = h.store.get(h.task.id)
        assert h.loop.run_once(task, h.clock.now) is RunOutcome.SKIPPED_OVERLAP


def test_terminal_completes_and_never_rescheduled(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", SUCCESS, sha="s1")
    assert h.tick() is RunOutcome.TERMINAL
    task = h.store.get(h.task.id)
    assert task.state is TaskState.COMPLETED
    assert CHECKS_PASSED in h.sink.kinds()
    assert h.store.list_schedulable(now=h.clock.now + 10_000) == []
    assert h.loop.run_once(task, h.clock.now) is RunOutcome.SKIPPED_TERMINAL


def test_events_recorded_to_activity_with_evidence(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", FAILURE, sha="s1")
    h.tick()
    entries = h.activity.query(task_id=h.task.id)
    assert [e.kind for e in entries] == [CHECKS_FAILED]
    assert entries[0].evidence["head_sha"] == "s1"
    assert CHECKS_FAILED in h.sink.kinds()


def test_secrets_never_in_activity(home: Path) -> None:
    from nanodot.core.redaction import Redactor
    from nanodot.native.secrets_file import FileSecretStore

    secret = "ghp_runnerleak777"
    FileSecretStore().set("github-token", secret)
    store = TaskStore(path=home / "nanodot.db", redactor=Redactor(FileSecretStore()))
    activity = ActivityLog(path=home / "nanodot.db", redactor=Redactor(FileSecretStore()))
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")
    sink = FakeSink()
    task = store.create(Task(target=TARGET, purpose=f"watch {secret}", next_check_at=0.0))
    loop = TaskLoop(store, github, sink, activity)
    loop.run_once(store.get(task.id), FakeClock().now)

    raw = (home / "nanodot.db").read_bytes()
    assert secret.encode() not in raw


def test_restart_recovers_without_duplicate_notifications(home: Path) -> None:
    # First process lifetime: observe a failure, notify once.
    h = Harness(home)
    h.github.add_check("ci", FAILURE, sha="s1")
    h.tick()
    assert h.sink.kinds() == [CHECKS_FAILED]
    notified_before = len(h.sink.events)
    h.store.close()
    h.activity.close()

    # "Restart": brand-new stores and loop over the same data home and the
    # same (unchanged) GitHub state; clock advanced past the cadence.
    h.clock.advance(CADENCE)
    store2 = TaskStore(path=home / "nanodot.db")
    activity2 = ActivityLog(path=home / "nanodot.db")
    sink2 = FakeSink()
    loop2 = TaskLoop(store2, h.github, sink2, activity2)
    from nanodot.native.daemon import RunnerDaemon

    daemon = RunnerDaemon(loop2, store2, clock=h.clock)
    daemon.tick()

    # No new notifications for the unchanged failure, and the activity log
    # holds exactly one checks-failed entry.
    assert len(sink2.events) == 0
    failures = activity2.query(task_id=h.task.id, kinds=(CHECKS_FAILED,))
    assert len(failures) == notified_before == 1
    store2.close()
    activity2.close()


def test_daemon_serve_stops_on_event(home: Path) -> None:
    h = Harness(home)
    h.github.add_check("ci", None, sha="s1", status="queued")
    stop = threading.Event()
    daemon = RunnerDaemon(h.loop, h.store, tick_seconds=0.01, clock=h.clock)

    def run() -> None:
        daemon.serve(stop, poll_seconds=0.001)

    thread = threading.Thread(target=run)
    thread.start()
    h.clock.advance(0)  # first tick happens immediately
    assert h.store.get(h.task.id).next_check_at is not None
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()

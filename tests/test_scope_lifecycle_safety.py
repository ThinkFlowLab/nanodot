"""Scope enforcement, recovery, and transition identity regressions."""

from dataclasses import replace
import sqlite3
from pathlib import Path

import pytest
from fakes import FakeGitHub, FAILURE, QUEUED, SUCCESS, TYPICAL_ERRORS
from test_runner import Harness

from nanodot.core.runner import CHECK_OBSERVED, RunOutcome, TaskLoop
from nanodot.core.statemachine import CHECKS_FAILED, CHECKS_PASSED, CHECKS_PENDING, step
from nanodot.core.tasks import (
    DEFAULT_STOP_CONDITIONS, SUPPORTED_NOTIFICATION_CONDITIONS,
    SUPPORTED_STOP_CONDITIONS, PRTarget, Task, TaskError, TaskState, TaskStore,
)
from nanodot.ports.github import RequiredCheck

TARGET = PRTarget.parse("owner/repo#1")
UNSUPPORTED = [
    ("notification_conditions", "only failures"),
    ("notification_conditions", "never notify"),
    ("stop_conditions", "stop on merge only"),
    ("stop_conditions", "never stop"),
]


def make_task(**overrides):
    return Task(**dict(target=TARGET, purpose="watch", next_check_at=1000.0, **overrides))


@pytest.mark.parametrize("notification_conditions", sorted(SUPPORTED_NOTIFICATION_CONDITIONS))
@pytest.mark.parametrize("stop_conditions", sorted(SUPPORTED_STOP_CONDITIONS))
def test_shipped_defaults_remain_supported(home, notification_conditions, stop_conditions):
    store = TaskStore()
    task = store.create(make_task(
        notification_conditions=notification_conditions, stop_conditions=stop_conditions,
    ))
    restored = store.get(task.id)
    assert restored.notification_conditions == notification_conditions
    assert restored.stop_conditions == stop_conditions
    assert store.list_schedulable(10000) == [restored]


@pytest.mark.parametrize("field,value", UNSUPPORTED)
def test_unsupported_scope_rejected_at_creation_and_update(home, field, value):
    store = TaskStore()
    invalid = make_task(**{field: value})
    with pytest.raises(TaskError, match="unsupported"):
        invalid.validate()
    with pytest.raises(TaskError, match="unsupported"):
        store.create(invalid)
    assert store.list() == []
    task = store.create(make_task())
    with pytest.raises(TaskError, match="unsupported"):
        store.update_scope(task.id, **{field: value})
    assert store.get(task.id).scope_version == 1
    setattr(task, field, value)
    with pytest.raises(TaskError, match="unsupported"):
        store.update(task)
    assert getattr(store.get(task.id), field) != value


@pytest.mark.parametrize("field,value", UNSUPPORTED)
def test_legacy_scope_is_inspectable_cancellable_but_not_scheduled(home, field, value):
    store = TaskStore()
    task = store.create(make_task())
    valid = store.create(make_task())
    with sqlite3.connect(home / "nanodot.db") as conn:
        conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, task.id))
    assert getattr(store.get(task.id), field) == value
    assert len(store.list()) == 2
    assert [t.id for t in store.list_schedulable(10000)] == [valid.id]
    blocked = store.get(task.id)
    assert blocked.state is TaskState.BLOCKED
    assert blocked.next_check_at is None
    assert "unsupported" in blocked.blocker
    assert getattr(blocked, field) == value
    with pytest.raises(TaskError, match="unsupported"):
        store.resume(task.id)
    assert store.pause(task.id).state is TaskState.PAUSED
    assert store.cancel(task.id).state is TaskState.CANCELLED


def test_explicit_scope_repair_can_resume_legacy_task(home):
    store = TaskStore()
    task = store.create(make_task())
    with sqlite3.connect(home / "nanodot.db") as conn:
        conn.execute("UPDATE tasks SET stop_conditions='forever' WHERE id=?", (task.id,))
    assert store.list_schedulable(10000) == []
    repaired = store.update_scope(task.id, stop_conditions=DEFAULT_STOP_CONDITIONS)
    assert repaired.state is TaskState.BLOCKED
    assert repaired.scope_version == 2
    resumed = store.resume(task.id, now=1234.0)
    assert resumed.next_check_at == 1234.0
    assert resumed.state is TaskState.ACTIVE
    assert resumed.blocker is None


@pytest.mark.parametrize("initial", ["pause", "set_blocked"])
def test_resume_schedules_without_manual_repair(home, initial):
    store = TaskStore()
    task = store.create(make_task())
    if initial == "set_blocked":
        store.set_blocked(task.id, "lost token")
    else:
        store.pause(task.id)
    assert store.get(task.id).next_check_at is None
    resumed = store.resume(task.id, now=2000.0)
    assert resumed.next_check_at == 2000.0
    assert store.list_schedulable(1999.0) == []
    assert store.list_schedulable(2000.0) == [resumed]


@pytest.mark.parametrize("terminal", ["complete", "cancel"])
def test_terminal_tasks_cannot_resurrect(home, terminal):
    store = TaskStore()
    task = store.create(make_task())
    getattr(store, terminal)(task.id)
    state = store.get(task.id).state
    for operation in (lambda: store.resume(task.id), lambda: store.pause(task.id),
                      lambda: store.update(task)):
        with pytest.raises(TaskError, match="create a new watch"):
            operation()
    assert store.get(task.id).state is state
    assert store.get(task.id).next_check_at is None


def test_duplicate_create_is_task_error_and_preserves_existing(home):
    store = TaskStore()
    task = store.create(make_task())
    with pytest.raises(TaskError, match="already exists"):
        store.create(replace(task, purpose="replacement"))
    assert store.get(task.id).purpose == task.purpose


def test_scope_error_does_not_echo_rejected_secret(home):
    store = TaskStore()
    secret = "ghp_rejected_scope_secret"
    with pytest.raises(TaskError) as error:
        store.create(make_task(notification_conditions=f"notify {secret}"))
    assert secret not in str(error.value)
    assert secret.encode() not in (home / "nanodot.db").read_bytes()


@pytest.mark.parametrize("field,value", UNSUPPORTED)
def test_direct_step_rejects_unsupported_scope(field, value):
    task = make_task(**{field: value})
    fake = FakeGitHub(TARGET)
    fake.set_pr("open", head_sha="s1")
    fake.add_check("ci", SUCCESS, sha="s1")
    with pytest.raises(TaskError, match="unsupported"):
        step(task, fake.snapshot(), now=1000.0)
    assert task.watch_state == {}


def test_failure_recurrence_gets_new_occurrence_on_same_sha():
    fake = FakeGitHub(TARGET)
    fake.set_pr("open", head_sha="s1")
    fake.add_check("ci", FAILURE, sha="s1")
    task = make_task()
    task.watch_state, first = step(task, fake.snapshot(), 1000.0)
    first_id = first[0].occurrence
    assert first_id
    fake.checks["s1"] = []
    fake.add_check("ci", None, sha="s1", status=QUEUED)
    task.watch_state, pending = step(task, fake.snapshot(), 1100.0)
    assert pending[0].occurrence != first_id
    fake.checks["s1"] = []
    fake.add_check("ci", FAILURE, sha="s1")
    task.watch_state, recurrence = step(task, fake.snapshot(), 1200.0)
    assert [e.kind for e in recurrence] == [CHECKS_FAILED]
    assert recurrence[0].occurrence != first_id
    assert recurrence[0].evidence == first[0].evidence
    _, unchanged = step(task, fake.snapshot(), 1300.0)
    assert unchanged == []


def test_uncommitted_transition_replay_has_stable_occurrence():
    fake = FakeGitHub(TARGET)
    fake.add_check("ci", FAILURE, sha="sha-1")
    task = make_task()
    first_state, first = step(task, fake.snapshot(), 1000.0)
    replay_state, replay = step(task, fake.snapshot(), 2000.0)
    assert replay[0].occurrence == first[0].occurrence
    assert replay_state["event_sequence"] == first_state["event_sequence"]
    assert task.watch_state == {}


@pytest.mark.parametrize("required,complete,message", [
    (None, True, "rules unavailable or unsupported"),
    ((RequiredCheck("ci"),), False, "results incomplete"),
    ((), True, "no required checks configured"),
])
def test_unknown_or_empty_required_rules_are_visible_without_success(required, complete, message):
    fake = FakeGitHub(TARGET)
    fake.add_check("ci", SUCCESS, sha="sha-1")
    snapshot = replace(fake.snapshot(), required_checks=required, checks_complete=complete)
    task = make_task()
    task.watch_state, events = step(task, snapshot, 1000.0)
    assert [e.kind for e in events] == [CHECKS_PENDING]
    assert message in events[0].message
    assert not events[0].terminal and not events[0].notable
    assert not task.watch_state.get("terminal")
    assert step(task, snapshot, 1100.0)[1] == []


@pytest.mark.parametrize("field,value", UNSUPPORTED)
def test_direct_runner_blocks_invalid_scope_before_fetch(home, field, value):
    h = Harness(home)
    h.github.add_check("ci", SUCCESS, sha="s1")
    setattr(h.task, field, value)
    assert h.loop.run_once(h.task, h.clock.now) is RunOutcome.BLOCKED
    assert h.github.fetch_calls == 0
    assert h.sink.events == []
    assert h.store.get(h.task.id).state is TaskState.BLOCKED


def test_runner_checks_saved_scope_even_with_stale_valid_input(home):
    h = Harness(home)
    with sqlite3.connect(home / "nanodot.db") as conn:
        conn.execute("UPDATE tasks SET notification_conditions='never notify' WHERE id=?", (h.task.id,))
    assert h.loop.run_once(h.task, h.clock.now) is RunOutcome.BLOCKED
    assert h.github.fetch_calls == 0
    assert h.sink.events == []
    assert "unsupported" in h.store.get(h.task.id).blocker


@pytest.mark.parametrize("lifecycle", ["pause", "cancel", "complete", "set_blocked"])
def test_stale_task_never_runs_after_saved_lifecycle_change(home, lifecycle):
    h = Harness(home)
    if lifecycle == "set_blocked":
        h.store.set_blocked(h.task.id, "action needed")
    else:
        getattr(h.store, lifecycle)(h.task.id)
    expected = RunOutcome.SKIPPED_TERMINAL if lifecycle in ("cancel", "complete") else RunOutcome.SKIPPED_INACTIVE
    assert h.loop.run_once(h.task, h.clock.now) is expected
    assert h.github.fetch_calls == 0
    assert h.sink.events == []


@pytest.mark.parametrize("change", ["pause", "cancel", "scope"])
@pytest.mark.parametrize("fetch_error", [None, "auth", "rate-limit"])
def test_lifecycle_change_during_fetch_prevents_delivery_and_stale_write(home, change, fetch_error):
    h = Harness(home)
    h.github.add_check("ci", SUCCESS, sha="s1")

    class ChangingFetcher:
        def fetch(self, target):
            if change == "scope":
                h.store.update_scope(h.task.id, purpose="scope edit probe")
            else:
                getattr(h.store, change)(h.task.id)
            if fetch_error:
                raise TYPICAL_ERRORS[fetch_error]
            return h.github.snapshot()

    h.loop = TaskLoop(h.store, ChangingFetcher(), h.sink, h.activity)
    expected = {
        "pause": RunOutcome.SKIPPED_INACTIVE,
        "cancel": RunOutcome.SKIPPED_TERMINAL,
        "scope": RunOutcome.SKIPPED_SCOPE_CHANGED,
    }[change]
    assert h.tick() is expected
    assert h.sink.events == []
    saved = h.store.get(h.task.id)
    if change == "scope":
        assert saved.purpose == "scope edit probe" and saved.scope_version == 2
    else:
        assert saved.state is (TaskState.PAUSED if change == "pause" else TaskState.CANCELLED)


def test_terminal_observation_failure_does_not_undo_completion(home, caplog):
    h = Harness(home)
    h.github.add_check("ci", SUCCESS, sha="s1")

    class FailingMemory:
        def add_observation(self, **kwargs):
            assert h.store.get(h.task.id).state is TaskState.COMPLETED
            raise RuntimeError("private memory contents")

    h.loop = TaskLoop(h.store, h.github, h.sink, h.activity, memory=FailingMemory())
    assert h.tick() is RunOutcome.TERMINAL
    saved = h.store.get(h.task.id)
    assert saved.state is TaskState.COMPLETED and saved.next_check_at is None
    assert h.store.list_schedulable(h.clock.now + 10000) == []
    assert h.tick() is RunOutcome.SKIPPED_TERMINAL
    assert h.sink.kinds() == [CHECKS_PASSED]
    assert "observation could not be saved" in caplog.text
    assert "private memory contents" not in caplog.text


def test_failure_occurrence_survives_restart(home):
    h = Harness(home)
    h.github.add_check("ci", FAILURE, sha="s1")
    h.tick()
    first_id = h.sink.events[0].occurrence
    h.github.checks["s1"] = []
    h.github.add_check("ci", None, sha="s1", status=QUEUED)
    h.tick()
    h.store.close()
    h.store = TaskStore(path=home / "nanodot.db")
    h.loop = TaskLoop(h.store, h.github, h.sink, h.activity)
    h.github.checks["s1"] = []
    h.github.add_check("ci", FAILURE, sha="s1")
    h.tick()
    assert h.sink.kinds() == [CHECKS_FAILED, CHECKS_FAILED]
    assert h.sink.events[-1].occurrence != first_id


@pytest.mark.parametrize("change", ["pause", "cancel", "scope"])
def test_lifecycle_change_during_summary_prevents_notification_and_write(home, change):
    h = Harness(home)
    h.github.add_check("ci", SUCCESS, sha="s1")

    class ChangingProvider:
        def summarize(self, change_event):
            if change == "scope":
                h.store.update_scope(h.task.id, purpose="scope edit probe")
            else:
                getattr(h.store, change)(h.task.id)
            return "finished"

    h.loop = TaskLoop(h.store, h.github, h.sink, h.activity, provider=ChangingProvider())
    expected = {
        "pause": RunOutcome.SKIPPED_INACTIVE,
        "cancel": RunOutcome.SKIPPED_TERMINAL,
        "scope": RunOutcome.SKIPPED_SCOPE_CHANGED,
    }[change]
    assert h.tick() is expected
    assert h.sink.events == []
    # Egress already happened (the summary was requested), so the log keeps
    # what the provider saw — adapter-seam discipline 3 — while delivery and
    # the task write stay suppressed for the superseded run.
    kinds = [e.kind for e in h.activity.query(task_id=h.task.id)]
    assert CHECK_OBSERVED in kinds and kinds[0] == CHECKS_PASSED
    saved = h.store.get(h.task.id)
    assert not saved.watch_state.get("terminal")


def test_stale_update_cannot_undo_pause_or_scope_change(home):
    store = TaskStore()
    task = store.create(make_task())
    store.pause(task.id)
    with pytest.raises(TaskError, match="no longer active"):
        store.update(task)
    assert store.get(task.id).state is TaskState.PAUSED
    store.resume(task.id, now=1000)
    store.update_scope(task.id, purpose="scope edit probe")
    with pytest.raises(TaskError, match="scope changed"):
        store.update(task)
    assert store.get(task.id).purpose == "scope edit probe"


def test_secret_target_is_rejected_on_create_update_and_execution(home):
    from nanodot.core.redaction import Redactor
    from nanodot.native.secrets_file import FileSecretStore
    from nanodot.core.activity import ActivityLog
    from fakes import FakeSink

    secret = "ghp_target_secret999"
    secrets = FileSecretStore()
    secrets.set("github-token", secret)
    store = TaskStore(redactor=Redactor(secrets))
    target = PRTarget.parse(f"{secret}/repo#1")
    with pytest.raises(TaskError, match="configured secret") as error:
        store.create(Task(target=target, purpose="watch", next_check_at=0))
    assert secret not in str(error.value)
    task = store.create(make_task())
    task.target = target
    with pytest.raises(TaskError, match="configured secret"):
        store.update(task)
    assert secret.encode() not in (home / "nanodot.db").read_bytes()

    # A newly configured secret can match an older stored target. Reads stay
    # inspectable, but both scheduling and a direct executor must block it.
    secrets.unset("github-token")
    legacy = store.create(Task(target=target, purpose="legacy", next_check_at=0))
    secrets.set("github-token", secret)
    fetcher, sink = FakeGitHub(target), FakeSink()
    loop = TaskLoop(store, fetcher, sink, ActivityLog())
    assert loop.run_once(legacy, now=1000) is RunOutcome.BLOCKED
    assert fetcher.fetch_calls == 0 and sink.events == []
    assert secret not in store.get(legacy.id).blocker
    assert store.cancel(legacy.id).state is TaskState.CANCELLED

    secrets.unset("github-token")
    legacy2 = store.create(Task(target=target, purpose="legacy 2", next_check_at=0))
    secrets.set("github-token", secret)
    assert legacy2.id not in [t.id for t in store.list_schedulable(1000)]
    assert store.get(legacy2.id).state is TaskState.BLOCKED


def test_empty_allowed_actions_cannot_authorize_a_read(home):
    store = TaskStore()
    with pytest.raises(TaskError, match="requires the read action"):
        store.create(make_task(allowed_actions=()))
    assert store.list() == []


@pytest.mark.parametrize("conclusion", ["error", "stale", "startup_failure"])
def test_failure_names_use_same_conclusions_as_evaluator(conclusion):
    task = make_task()
    github = FakeGitHub(TARGET)
    github.add_check("required-job", conclusion, sha=github.head_sha)
    _, events = step(task, github.snapshot(), now=1000)
    failed = next(event for event in events if event.kind == CHECKS_FAILED)
    assert "required-job" in failed.message

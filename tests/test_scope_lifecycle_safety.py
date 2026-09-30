"""Scope enforcement, recovery, and transition identity regressions."""

from dataclasses import replace
import sqlite3

import pytest
from nanodot.core.tasks import (
    DEFAULT_STOP_CONDITIONS, SUPPORTED_NOTIFICATION_CONDITIONS,
    SUPPORTED_STOP_CONDITIONS, PRTarget, Task, TaskError, TaskState, TaskStore,
)


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


def test_stale_update_cannot_undo_pause_or_scope_change(home):
    store = TaskStore()
    task = store.create(make_task())
    store.pause(task.id)
    with pytest.raises(TaskError, match="no longer active"):
        store.update(task)
    assert store.get(task.id).state is TaskState.PAUSED
    store.resume(task.id, now=1000)
    store.update_scope(task.id, cadence_seconds=600)
    with pytest.raises(TaskError, match="scope changed"):
        store.update(task)
    assert store.get(task.id).cadence_seconds == 600


def test_empty_allowed_actions_cannot_authorize_a_read(home):
    store = TaskStore()
    with pytest.raises(TaskError, match="requires the read action"):
        store.create(make_task(allowed_actions=()))
    assert store.list() == []


def test_secret_target_rejected_on_create_update_and_scheduling(home):
    from nanodot.core.redaction import Redactor
    from nanodot.native.secrets_file import FileSecretStore

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
    secrets.unset("github-token")
    legacy = store.create(Task(target=target, purpose="legacy", next_check_at=0))
    secrets.set("github-token", secret)
    assert legacy.id not in [t.id for t in store.list_schedulable(1000)]
    assert store.get(legacy.id).state is TaskState.BLOCKED
    assert secret not in store.get(legacy.id).blocker
    assert store.cancel(legacy.id).state is TaskState.CANCELLED


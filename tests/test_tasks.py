"""Task store and model acceptance tests (issue #5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nanodot.core.redaction import Redactor
from nanodot.core.tasks import (
    DEFAULT_NOTIFICATION_CONDITIONS, DEFAULT_STOP_CONDITIONS,
    PRTarget, Task, TaskError, TaskState, TaskStore,
)
from nanodot.native.secrets_file import FileSecretStore


def make_task(**overrides) -> Task:
    defaults = dict(
        target=PRTarget.parse("thinkflowlab/nanodot#1"),
        purpose="tell me when required checks pass",
        next_check_at=1000.0,
    )
    defaults.update(overrides)
    return Task(**defaults)


def test_target_parsing() -> None:
    target = PRTarget.parse("owner/repo#42")
    assert (target.owner, target.repo, target.number) == ("owner", "repo", 42)
    with pytest.raises(TaskError):
        PRTarget.parse("not-a-target")
    with pytest.raises(TaskError):
        PRTarget.parse("owner/repo#abc")


def test_create_read_update_roundtrip(home: Path) -> None:
    store = TaskStore()
    task = store.create(make_task())
    loaded = store.get(task.id)
    assert loaded is not None
    assert loaded.target == task.target
    assert loaded.purpose == task.purpose
    assert loaded.state is TaskState.ACTIVE

    loaded.purpose = "updated purpose"
    store.update(loaded)
    assert store.get(task.id).purpose == "updated purpose"


def test_state_survives_restart(home: Path) -> None:
    store = TaskStore()
    task = store.create(make_task())
    store.pause(task.id)
    store.close()

    reopened = TaskStore()
    paused = reopened.get(task.id)
    assert paused.state is TaskState.PAUSED
    reopened.cancel(task.id)
    reopened.close()

    reopened_again = TaskStore()
    assert reopened_again.get(task.id).state is TaskState.CANCELLED


def test_terminal_and_paused_tasks_never_scheduled(home: Path) -> None:
    store = TaskStore()
    active = store.create(make_task())
    paused = store.create(make_task())
    done = store.create(make_task())
    store.pause(paused.id)
    store.complete(done.id)

    schedulable = {task.id for task in store.list_schedulable(now=10_000)}
    assert active.id in schedulable
    assert paused.id not in schedulable
    assert done.id not in schedulable


def test_validation_rejects_write_actions(home: Path) -> None:
    with pytest.raises(TaskError, match="read-only"):
        make_task(allowed_actions=("read", "comment")).validate()
    with pytest.raises(TaskError, match="read-only"):
        make_task(allowed_actions=("merge",)).validate()
    store = TaskStore()
    with pytest.raises(TaskError):
        store.create(make_task(allowed_actions=("rerun",)))
    with pytest.raises(TaskError):
        make_task(purpose="   ").validate()
    with pytest.raises(TaskError):
        make_task(cadence_seconds=0).validate()


def test_scope_stored_verbatim_and_version_bumps(home: Path) -> None:
    store = TaskStore()
    task = store.create(
        make_task(
            notification_conditions=DEFAULT_NOTIFICATION_CONDITIONS,
            stop_conditions=DEFAULT_STOP_CONDITIONS,
        )
    )
    loaded = store.get(task.id)
    assert loaded.notification_conditions == DEFAULT_NOTIFICATION_CONDITIONS
    assert loaded.stop_conditions == DEFAULT_STOP_CONDITIONS
    assert loaded.scope_version == 1

    changed = store.update_scope(task.id, cadence_seconds=600)
    assert changed.cadence_seconds == 600
    assert changed.scope_version == 2


def test_blocked_state_carries_reason(home: Path) -> None:
    store = TaskStore()
    task = store.create(make_task())
    blocked = store.set_blocked(task.id, "lost GitHub authorization")
    assert blocked.state is TaskState.BLOCKED
    assert "authorization" in blocked.blocker
    resumed = store.resume(task.id)
    assert resumed.state is TaskState.ACTIVE
    assert resumed.blocker is None


def test_storage_is_inspectable_with_standard_tools(home: Path) -> None:
    store = TaskStore()
    store.create(make_task())
    store.close()
    conn = sqlite3.connect(home / "nanodot.db")
    count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    conn.close()
    assert count == 1


def test_secrets_never_persist_in_task_text(home: Path) -> None:
    secret = "ghp_nevertaskleak999"
    FileSecretStore().set("github-token", secret)
    store = TaskStore(redactor=Redactor(FileSecretStore()))
    task = store.create(
        make_task(
            purpose=f"watch using {secret}",
            blocker=None,
        )
    )
    store.set_blocked(task.id, f"token {secret} rejected")
    raw = (home / "nanodot.db").read_bytes()
    assert secret.encode() not in raw
    loaded = store.get(task.id)
    assert secret not in loaded.purpose
    assert secret not in loaded.notification_conditions
    assert secret not in (loaded.blocker or "")

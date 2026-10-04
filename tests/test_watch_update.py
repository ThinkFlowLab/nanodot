"""watch update acceptance tests (issue #95): operational knobs adjust in
place without grant invalidation; scope changes still bump scope_version."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from nanodot.core.permissions import PermissionCenter
from nanodot.core.tasks import PRTarget, Task, TaskError, TaskStore


def make_store(home: Path) -> TaskStore:
    return TaskStore(path=home / "nanodot.db")


def make_task(store: TaskStore) -> Task:
    return store.create(
        Task(target=PRTarget.parse("thinkflowlab/nanodot#95"), purpose="p")
    )


def _add_watch(home: Path, *extra: str) -> int:
    from nanodot.cli import main
    from nanodot.native.secrets_file import FileSecretStore

    FileSecretStore().set("github-token", "ghp_x")
    with mock.patch("builtins.input", return_value="y"):
        return main(["watch", "add", "thinkflowlab/nanodot#95", "--yes", *extra])


# -- store semantics --------------------------------------------------------------


def test_tuning_updates_without_scope_bump(home: Path) -> None:
    store = make_store(home)
    task = make_task(store)
    updated = store.update_tuning(
        task.id,
        cadence_seconds=120,
        digest_interval_seconds=21600,
        stale_after_seconds=172800,
        flaky_alerts=True,
    )
    assert updated.cadence_seconds == 120
    assert updated.digest_interval_seconds == 21600
    assert updated.stale_after_seconds == 172800
    assert updated.flaky_alerts is True
    assert updated.scope_version == 1  # operational, not authorization scope
    assert updated.state.value == "active"


def test_tuning_off_clears_dimensions(home: Path) -> None:
    store = make_store(home)
    task = store.create(
        Task(
            target=PRTarget.parse("a/b#1"),
            purpose="p",
            digest_interval_seconds=86400,
            stale_after_seconds=604800,
            flaky_alerts=True,
        )
    )
    updated = store.update_tuning(
        task.id, digest_interval_seconds="off", stale_after_seconds="off",
        flaky_alerts=False,
    )
    assert updated.digest_interval_seconds is None
    assert updated.stale_after_seconds is None
    assert updated.flaky_alerts is False


def test_tuning_validates_enums(home: Path) -> None:
    store = make_store(home)
    task = make_task(store)
    with pytest.raises(TaskError):
        store.update_tuning(task.id, digest_interval_seconds=12345)
    with pytest.raises(TaskError):
        store.update_tuning(task.id, stale_after_seconds=99)


def test_tuning_rejects_terminal_allows_paused(home: Path) -> None:
    store = make_store(home)
    task = make_task(store)
    store.pause(task.id)
    assert store.update_tuning(task.id, cadence_seconds=60).cadence_seconds == 60
    store.cancel(task.id)
    with pytest.raises(TaskError):
        store.update_tuning(task.id, cadence_seconds=60)


def test_grants_survive_tuning_and_die_on_scope_change(home: Path) -> None:
    store = make_store(home)
    center = PermissionCenter(path=home / "nanodot.db")
    task = make_task(store)
    req = center.request("comment", str(task.target), "watch", task.id)
    center.approve(req.id)
    assert center.permits("comment", str(task.target), "watch", task.id)

    store.update_tuning(task.id, cadence_seconds=99, flaky_alerts=True)
    assert center.permits("comment", str(task.target), "watch", task.id)  # survive

    store.update_scope(task.id, purpose="new purpose")
    assert not center.permits("comment", str(task.target), "watch", task.id)  # die


# -- CLI surface --------------------------------------------------------------------


def test_cli_update_each_knob(home: Path, capsys) -> None:
    assert _add_watch(home) == 0
    task = TaskStore().list()[0]

    from nanodot.cli import main

    assert main([
        "watch", "update", task.id,
        "--cadence", "120", "--digest", "24h", "--stale", "7d", "--flaky", "on",
    ]) == 0
    updated = TaskStore().get(task.id)
    assert updated.cadence_seconds == 120
    assert updated.digest_interval_seconds == 86400
    assert updated.stale_after_seconds == 604800
    assert updated.flaky_alerts is True
    assert updated.scope_version == 1
    out = capsys.readouterr().out
    assert "grants unaffected" in out and "digest: 24h" in out

    assert main(["watch", "update", task.id, "--digest", "off"]) == 0
    assert TaskStore().get(task.id).digest_interval_seconds is None


def test_cli_update_rejects_noop_and_bad_id(home: Path, capsys) -> None:
    from nanodot.cli import main

    assert _add_watch(home) == 0
    task = TaskStore().list()[0]
    assert main(["watch", "update", task.id]) == 1
    assert "nothing to update" in capsys.readouterr().err
    assert main(["watch", "update", "no-such-id", "--cadence", "60"]) == 1
    assert "no such task" in capsys.readouterr().err


def test_cli_update_invalid_value_rejected_at_parse(home: Path) -> None:
    from nanodot.cli import main

    with pytest.raises(SystemExit):
        main(["watch", "update", "x", "--digest", "2h"])  # not in choices

"""CLI acceptance tests (issue #10)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest

from nanodot.cli import main
from nanodot.core.activity import ActivityLog
from nanodot.core.redaction import Redactor
from nanodot.core.tasks import TaskStore
from nanodot.native.secrets_file import FileSecretStore

TARGET = "thinkflowlab/nanodot#1"


@pytest.fixture()
def token(home: Path) -> str:
    value = "ghp_clitoken123"
    FileSecretStore().set("github-token", value)
    return value


def _add_watch(confirm: str = "y") -> int:
    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")  # input() echoes its prompt to stdout
        return confirm

    with mock.patch("builtins.input", side_effect=fake_input):
        return main(["watch", "add", TARGET, "--cadence", "120"])


def test_watch_add_displays_full_scope_and_confirms(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch("y") == 0
    out = capsys.readouterr().out
    for field in (
        "target:",
        "purpose:",
        "cadence:",
        "allowed actions:",
        "notification conditions:",
        "stop conditions:",
    ):
        assert field in out
    assert "Proceed?" in out
    tasks = TaskStore().list()
    assert len(tasks) == 1
    assert tasks[0].cadence_seconds == 120
    assert tasks[0].next_check_at is not None


def test_watch_add_declined_persists_nothing(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch("n") == 1
    assert TaskStore().list() == []


def test_watch_add_requires_token(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["watch", "add", TARGET]) == 1
    assert "github token" in capsys.readouterr().err.lower()


def test_watch_add_rejects_bad_target(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["watch", "add", "garbage", "--yes"]) == 1
    assert "invalid PR target" in capsys.readouterr().err


def test_pause_resume_cancel_take_effect(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch() == 0
    task_id = TaskStore().list()[0].id

    assert main(["watch", "pause", task_id]) == 0
    assert TaskStore().get(task_id).state.value == "paused"
    assert main(["watch", "resume", task_id]) == 0
    assert TaskStore().get(task_id).state.value == "active"
    assert main(["watch", "cancel", task_id]) == 0
    assert TaskStore().get(task_id).state.value == "cancelled"
    assert main(["watch", "pause", "no-such-id"]) == 1


def test_watch_list_shows_status_latest_next_blocker(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch() == 0
    store = TaskStore()
    task = store.list()[0]
    ActivityLog().append(task.id, "checks-failed", "ci failing on abc", at=time.time())
    store.flag(task.id, "prolonged fetch failure: no success for 4000s")

    assert main(["watch", "list"]) == 0
    out = capsys.readouterr().out
    assert task.id in out and "active" in out
    assert "ci failing" in out
    assert "blocked:" in out and "prolonged" in out


def test_activity_renders_history_without_secrets(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch() == 0
    task = TaskStore().list()[0]
    ActivityLog(redactor=Redactor(FileSecretStore())).append(
        task.id, "checks-failed", f"failure with {token}", at=1000.0
    )

    assert main(["activity", task.id]) == 0
    out = capsys.readouterr().out
    assert "checks-failed" in out and "failure with" in out
    assert token not in out


def test_runner_once_without_token_fails_clearly(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch() == 0
    FileSecretStore().unset("github-token")

    assert main(["runner", "--once"]) == 1
    err = capsys.readouterr().err
    assert "blocked" in err
    assert "token" in err.lower() or "authorization" in err.lower()


def test_start_status_stop_roundtrip(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch() == 0
    # No token in the runner: ticks hit the auth blocker without any
    # network access, exercising lifecycle only.
    FileSecretStore().unset("github-token")

    assert main(["start"]) == 0
    capsys.readouterr()
    try:
        deadline = time.time() + 10
        running = False
        while time.time() < deadline:
            if main(["status"]) == 0:
                running = True
                break
            time.sleep(0.2)
        assert running, "runner never came up"
    finally:
        assert main(["stop"]) == 0
        time.sleep(0.3)
    assert main(["status"]) == 1
    capsys.readouterr()

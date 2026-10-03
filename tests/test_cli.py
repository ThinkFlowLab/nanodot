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
    assert f"paused {task_id}" in capsys.readouterr().out
    assert TaskStore().get(task_id).state.value == "paused"
    assert main(["watch", "resume", task_id]) == 0
    assert f"resumed {task_id}" in capsys.readouterr().out
    assert TaskStore().get(task_id).state.value == "active"
    assert main(["watch", "cancel", task_id]) == 0
    out = capsys.readouterr().out
    assert f"cancelled {task_id}" in out and "canceld" not in out
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


def _corrupt_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(text)


def test_config_list_survives_invalid_stored_value(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _corrupt_config(home, '{"os-notifications": "off"}')
    assert main(["config", "list"]) == 0
    out = capsys.readouterr().out
    assert "os-notifications=<invalid:" in out
    assert "must be true or false" in out


def test_config_list_fails_cleanly_on_truncated_json(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _corrupt_config(home, '{"os-notifications": tru')
    assert main(["config", "list"]) == 1
    assert "error:" in capsys.readouterr().err


def test_config_unset_fails_cleanly_on_non_object_config(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _corrupt_config(home, "[]")
    assert main(["config", "unset", "github-auth-mode"]) == 1
    assert "JSON object" in capsys.readouterr().err


def test_watch_add_reports_unusable_secret_store_cleanly(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    victim = home.parent / "outside-secrets.json"
    victim.write_text("{}")
    (home / "secrets.json").symlink_to(victim)
    assert main(["watch", "add", TARGET, "--yes"]) == 1
    assert "regular file" in capsys.readouterr().err
    assert victim.read_text() == "{}"


def test_config_set_secret_reports_unusable_secret_store_cleanly(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    victim = home.parent / "outside-token.json"
    victim.write_text("{}")
    (home / "secrets.json").symlink_to(victim)
    assert main(["config", "set", "github-token", "ghp_whatever"]) == 1
    assert "regular file" in capsys.readouterr().err


def test_watch_add_confirmation_declines_on_eof_stdin(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with mock.patch("builtins.input", side_effect=EOFError):
        assert main(["watch", "add", TARGET]) == 1
    assert "cancelled" in capsys.readouterr().out
    assert TaskStore().list() == []


def test_concurrent_config_and_secret_writes_keep_all_keys(home: Path) -> None:
    import threading

    from nanodot.core.config import Config

    home.mkdir(parents=True, exist_ok=True)
    Config().set("github-auth-mode", "anonymous")
    errors: list[Exception] = []

    def config_writer(index: int) -> None:
        try:
            for round_index in range(25):
                Config().set(f"key-{index}", f"value-{round_index}")
        except Exception as error:  # pragma: no cover - surfaced via assert
            errors.append(error)

    def secret_writer(index: int) -> None:
        try:
            for round_index in range(25):
                FileSecretStore().set(f"secret-{index}", f"value-{round_index}")
        except Exception as error:  # pragma: no cover - surfaced via assert
            errors.append(error)

    def reader() -> None:
        try:
            for _ in range(200):
                Config().get("github-auth-mode")  # never a partial file
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=config_writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=secret_writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    final = Config()
    assert final.get("github-auth-mode") == "anonymous"
    for index in range(4):
        assert final.get(f"key-{index}") is not None
    store = FileSecretStore()
    for index in range(4):
        assert store.get(f"secret-{index}") is not None


def test_activity_hides_observations_unless_all(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_watch() == 0
    task = TaskStore().list()[0]
    log = ActivityLog()
    log.append(task.id, "check-observed", "observed abc1234567 (open)", at=1000.0)
    log.append(task.id, "checks-failed", "ci failing on abc", at=1000.0)

    assert main(["activity", task.id]) == 0
    out = capsys.readouterr().out
    assert "checks-failed" in out
    assert "check-observed" not in out

    assert main(["activity", task.id, "--all"]) == 0
    out = capsys.readouterr().out
    assert "check-observed" in out and "checks-failed" in out


def test_approvals_approve_and_denied_never_reasked(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from nanodot.core.permissions import PermissionCenter

    center = PermissionCenter()
    request = center.request(
        action="comment", target=TARGET, scope="watch:s1",
        task_id="task-1", content={"body": "the exact approved text"},
    )

    assert main(["approvals", "list"]) == 0
    out = capsys.readouterr().out
    assert request.id in out and "the exact approved text" in out

    assert main(["approvals", "approve", request.id]) == 0
    out = capsys.readouterr().out
    assert "approved comment" in out and "single use" in out

    # The approval left an activity trail.
    entries = ActivityLog().query(task_id="task-1", kinds=("write-approved",))
    assert entries and entries[0].evidence["content_hash"]

    # Denial of a second request is terminal: no verbatim re-ask.
    second = center.request(
        action="comment", target=TARGET, scope="watch:s1",
        task_id="task-1", content={"body": "the exact approved text"},
    )
    assert main(["approvals", "deny", second.id]) == 0
    assert "denied" in capsys.readouterr().out
    from nanodot.ports.github_writer import payload_digest

    assert center.has_verbatim_request(
        "task-1", payload_digest({"body": "the exact approved text"})
    ) == "denied"


def test_watch_add_digest_persists_and_shows(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_input(prompt: str = "") -> str:
        return "y"

    with mock.patch("builtins.input", side_effect=fake_input):
        assert main(["watch", "add", TARGET, "--digest", "24h"]) == 0
    out = capsys.readouterr().out
    assert "digest:" in out  # preview line
    task = TaskStore().list()[0]
    assert task.digest_interval_seconds == 86400

    assert main(["watch", "show", task.id]) == 0
    assert "digest:" in capsys.readouterr().out

    # Invalid values are rejected by the fixed-enum parser before anything
    # is persisted (argparse exits 2).
    with mock.patch("builtins.input", side_effect=fake_input):
        with pytest.raises(SystemExit) as caught:
            main(["watch", "add", TARGET, "--digest", "2d"])
    assert caught.value.code == 2
    assert len(TaskStore().list()) == 1


def test_watch_add_stale_persists_and_shows(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_input(prompt: str = "") -> str:
        return "y"

    with mock.patch("builtins.input", side_effect=fake_input):
        assert main(["watch", "add", TARGET, "--stale", "3d", "--digest", "24h"]) == 0
    out = capsys.readouterr().out
    assert "stale alert:" in out and "digest:" in out
    task = TaskStore().list()[0]
    assert task.stale_after_seconds == 259200
    assert task.digest_interval_seconds == 86400

    assert main(["watch", "show", task.id]) == 0
    assert "stale alert:" in capsys.readouterr().out


def test_flaky_flag_persists_previews_and_shows(
    home: Path, token: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_input(prompt: str = "") -> str:
        return "y"

    with mock.patch("builtins.input", side_effect=fake_input):
        assert main(["watch", "add", TARGET, "--flaky", "--stale", "3d"]) == 0
    out = capsys.readouterr().out
    assert "flaky alerts:" in out and "stale alert:" in out
    task = TaskStore().list()[0]
    assert task.flaky_alerts is True

    assert main(["watch", "show", task.id]) == 0
    assert "flaky alerts:" in capsys.readouterr().out

    with mock.patch("builtins.input", side_effect=fake_input):
        with pytest.raises(SystemExit) as caught:
            main(["watch", "add", TARGET, "--stale", "5d"])
    assert caught.value.code == 2

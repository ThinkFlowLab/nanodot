"""Explicit public-only first use and typed notification configuration."""

from __future__ import annotations

import io
import json
from pathlib import Path
import threading
from unittest import mock
from urllib.error import HTTPError

import pytest

from nanodot.cli import _os_notifications_enabled, main
from nanodot.core.config import Config
from nanodot.core.github_eval import CheckOutcome, evaluate_checks
from nanodot.core.tasks import PRTarget, Task, TaskState, TaskStore
from nanodot.native.github_client import GitHubSnapshotFetcher
from nanodot.native.notifier import NativeNotifier
from nanodot.native.runner_control import (
    RunnerAlreadyRunning, RunnerControlError, RunnerLease, configuration_lock,
    stop_runner,
)
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github import AuthLostError, PRNotFoundError, RetryableError

TARGET = PRTarget.parse("thinkflowlab/nanodot#7")


def _response(body: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(body).encode())


def _pull(*, merged: bool = True) -> dict:
    return {
        "head": {"sha": "abc123"}, "base": {"ref": "main"},
        "state": "closed" if merged else "open", "merged": merged,
    }


def test_anonymous_cli_add_needs_no_token(home: Path, capsys) -> None:
    assert main(["config", "set", "github-auth-mode", "anonymous"]) == 0
    assert main(["watch", "add", str(TARGET), "--yes"]) == 0
    assert "anonymous (public repositories only)" in capsys.readouterr().out
    assert TaskStore().list()[0].target == TARGET
    assert FileSecretStore().get("github-token") is None
    assert Config().get("github-auth-mode") == "anonymous"


@pytest.mark.parametrize("inline_token", [None, "explicit-token-must-be-ignored"])
def test_anonymous_fetch_never_reads_or_sends_any_token(home: Path, inline_token) -> None:
    FileSecretStore().set("github-token", "saved-token-must-be-ignored")
    fetcher = GitHubSnapshotFetcher(token=inline_token, auth_mode="anonymous")
    with (
        mock.patch.object(FileSecretStore, "get", side_effect=AssertionError("token read")),
        mock.patch("nanodot.native.github_client.authenticated_urlopen",
                   return_value=_response(_pull())) as transport,
    ):
        assert fetcher.fetch(TARGET).pr_state == "merged"
    request = transport.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.full_url == "https://api.github.com/repos/thinkflowlab/nanodot/pulls/7"
    assert not request.has_header("Authorization")
    assert request.get_header("Accept") == "application/vnd.github+json"


def test_saved_anonymous_mode_is_used_by_runner(home: Path, capsys) -> None:
    assert main(["config", "set", "github-auth-mode", "anonymous"]) == 0
    assert main(["config", "set", "os-notifications", "false"]) == 0
    # An already-saved credential must never silently switch this run to auth.
    FileSecretStore().set("github-token", "saved-unused-token")
    assert main(["watch", "add", str(TARGET), "--yes"]) == 0
    calls = []

    def fetch(request, timeout):
        calls.append(request)
        assert not request.has_header("Authorization")
        return _response(_pull())

    with (
        mock.patch("nanodot.native.github_client.authenticated_urlopen", fetch),
        mock.patch("nanodot.native.notifier._default_osascript") as os_delivery,
    ):
        assert main(["runner", "--once"]) == 0
        assert main(["runner", "--once"]) == 0
    assert len(calls) == 1
    assert TaskStore().list()[0].state.value == "completed"
    sink = NativeNotifier(os_notify=False)
    assert len(sink.list()) == 1
    assert sink.list()[0].kind == "pr-merged"
    sink.close()
    os_delivery.assert_not_called()
    assert "ran 0 task(s)" in capsys.readouterr().out


def test_once_runner_stop_during_fetch_skips_remaining_tasks(home: Path, capsys) -> None:
    Config().set("github-auth-mode", "anonymous")
    Config().set("os-notifications", False)
    store = TaskStore()
    tasks = [
        store.create(Task(
            target=PRTarget.parse(f"o/r#{number}"), purpose="watch", next_check_at=number,
        ))
        for number in range(1, 4)
    ]
    stop_events = []
    calls = []

    def lease(pidfile, stop, *, prepare):
        stop_events.append(stop)
        return RunnerLease(pidfile, stop, prepare=prepare)

    def fetch(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            # Request an actual cooperative stop while this fetch is in
            # flight. Ownership cannot be released until its result commits.
            with pytest.raises(RunnerControlError, match="stop is still pending"):
                stop_runner(home / "runner.pid", timeout=0)
            assert stop_events[0].wait(2)
        return _response(_pull())

    with (
        mock.patch("nanodot.native.runner_control.RunnerLease", side_effect=lease),
        mock.patch("nanodot.native.github_client.authenticated_urlopen", fetch),
    ):
        assert main(["runner", "--once"]) == 0

    assert calls == ["https://api.github.com/repos/o/r/pulls/1"]
    assert "ran 1 task(s)" in capsys.readouterr().out
    assert not (home / "runner.pid").exists()
    assert not (home / "runner.stop").exists()
    first = store.get(tasks[0].id)
    assert first.state is TaskState.COMPLETED
    assert first.next_check_at is None
    assert first.watch_state["terminal"]
    for untouched in tasks[1:]:
        assert store.get(untouched.id) == untouched
    sink = NativeNotifier(os_notify=False)
    assert [entry.task_id for entry in sink.list()] == [first.id]
    sink.close()
    store.close()


@pytest.mark.parametrize("inline", [False, True])
def test_default_token_mode_still_sends_token(home: Path, inline: bool) -> None:
    FileSecretStore().set("github-token", "saved-token")
    fetcher = GitHubSnapshotFetcher(token="inline-token" if inline else None)
    with mock.patch("nanodot.native.github_client.authenticated_urlopen",
                    return_value=_response(_pull())) as transport:
        assert fetcher.fetch(TARGET).pr_state == "merged"
    assert transport.call_args.args[0].get_header("Authorization") == (
        "Bearer inline-token" if inline else "Bearer saved-token"
    )


def test_unsetting_anonymous_mode_restores_missing_token_blocker(home: Path, capsys) -> None:
    assert main(["config", "set", "github-auth-mode", "anonymous"]) == 0
    assert main(["config", "unset", "github-auth-mode"]) == 0
    assert main(["watch", "add", str(TARGET), "--yes"]) == 1
    assert "no GitHub token" in capsys.readouterr().err
    assert TaskStore().list() == []
    with pytest.raises(AuthLostError, match="no GitHub token"):
        GitHubSnapshotFetcher().fetch(TARGET)


@pytest.mark.parametrize("value", ["", "public", "TOKEN", None, False, 1, []])
def test_invalid_auth_mode_fails_closed(home: Path, value, capsys) -> None:
    with pytest.raises(ValueError, match="github-auth-mode"):
        Config().set("github-auth-mode", value)
    with pytest.raises(ValueError, match="github-auth-mode"):
        GitHubSnapshotFetcher(auth_mode=value)
    (home / "config.json").write_text(json.dumps({"github-auth-mode": value}))
    assert main(["watch", "add", str(TARGET), "--yes"]) == 1
    assert main(["runner", "--once"]) == 1
    assert "github-auth-mode must be" in capsys.readouterr().err
    assert TaskStore().list() == []


def test_invalid_auth_mode_cli_set_preserves_previous_value(home: Path, capsys) -> None:
    assert main(["config", "set", "github-auth-mode", "anonymous"]) == 0
    assert main(["config", "set", "github-auth-mode", "auto"]) == 1
    assert Config().get("github-auth-mode") == "anonymous"
    assert "github-auth-mode must be" in capsys.readouterr().err


@pytest.mark.parametrize("code,headers,error", [
    (401, {}, AuthLostError),
    (404, {}, PRNotFoundError),
    (403, {"X-RateLimit-Remaining": "0"}, RetryableError),
])
def test_anonymous_errors_do_not_fall_back_to_saved_credentials(home: Path, code, headers, error) -> None:
    FileSecretStore().set("github-token", "never-retry-with-this")
    failure = HTTPError("https://api.github.com", code, "blocked", headers, io.BytesIO(b"{}"))
    with (
        mock.patch.object(FileSecretStore, "get", side_effect=AssertionError("token read")),
        mock.patch("nanodot.native.github_client.authenticated_urlopen",
                   side_effect=failure) as transport,
        pytest.raises(error),
    ):
        GitHubSnapshotFetcher(auth_mode="anonymous").fetch(TARGET)
    assert transport.call_count == 1
    assert not transport.call_args.args[0].has_header("Authorization")


def test_anonymous_hidden_requirements_never_imply_passing(home: Path) -> None:
    def fetch(request, timeout):
        assert not request.has_header("Authorization")
        if "/pulls/" in request.full_url:
            return _response(_pull(merged=False))
        if "/branches/" in request.full_url:
            raise HTTPError(request.full_url, 404, "hidden", {}, io.BytesIO(b"{}"))
        if "/check-suites" in request.full_url:
            return _response({"total_count": 0, "check_suites": []})
        if "/status?" in request.full_url:
            return _response({"sha": "abc123", "total_count": 1, "statuses": [
                {"id": 1, "context": "ci", "state": "success"},
            ]})
        raise AssertionError(request.full_url)

    with mock.patch("nanodot.native.github_client.authenticated_urlopen", fetch):
        snapshot = GitHubSnapshotFetcher(auth_mode="anonymous").fetch(TARGET)
    assert snapshot.required_checks is None
    assert snapshot.checks_complete
    assert evaluate_checks(snapshot) is CheckOutcome.PENDING


@pytest.mark.parametrize("text,expected", [("false", False), ("true", True), ("FALSE", False)])
def test_notification_config_is_persisted_as_boolean(home: Path, text: str, expected: bool) -> None:
    assert main(["config", "set", "os-notifications", text]) == 0
    assert json.loads((home / "config.json").read_text())["os-notifications"] is expected
    assert Config().get("os-notifications") is expected
    assert _os_notifications_enabled() is expected


def test_legacy_false_string_disables_actual_os_delivery(home: Path) -> None:
    from nanodot.cli import _wiring
    from nanodot.core.statemachine import WatchEvent

    home.mkdir()
    (home / "config.json").write_text(json.dumps({"os-notifications": "false"}))
    assert _os_notifications_enabled() is False
    with mock.patch("nanodot.native.notifier._default_osascript") as os_delivery:
        _, store, activity, sink, _, loop = _wiring()
        sink.notify(WatchEvent(kind="pr-merged", message="merged", task_id="t1"))
    assert len(sink.list()) == 1
    os_delivery.assert_not_called()
    store.close()
    activity.close()
    sink.close()
    loop._memory.close()


@pytest.mark.parametrize("value", ["off", "yes", "", 0, 1, None, []])
def test_invalid_notification_values_fail_closed(home: Path, value, capsys) -> None:
    with pytest.raises(ValueError, match="os-notifications"):
        Config().set("os-notifications", value)
    (home / "config.json").write_text(json.dumps({"os-notifications": value}))
    assert main(["runner", "--once"]) == 1
    assert "os-notifications must be true or false" in capsys.readouterr().err


def test_non_object_configuration_fails_closed(home: Path, capsys) -> None:
    home.mkdir()
    (home / "config.json").write_text("[]")
    assert main(["watch", "add", str(TARGET), "--yes"]) == 1
    assert main(["runner", "--once"]) == 1
    assert "config.json must contain a JSON object" in capsys.readouterr().err


def test_notifications_default_to_enabled(home: Path) -> None:
    assert _os_notifications_enabled() is True


@pytest.mark.parametrize("key,initial,replacement", [
    ("github-auth-mode", "token", "anonymous"),
    ("os-notifications", True, "false"),
])
@pytest.mark.parametrize("command", ["set", "unset"])
def test_runner_policy_changes_require_stopped_runner(
    home: Path, capsys, key: str, initial, replacement: str, command: str,
) -> None:
    Config().set(key, initial)
    args = ["config", command, key]
    if command == "set":
        args.append(replacement)

    # Use the actual lifetime flock and metadata rather than mocking status.
    with RunnerLease(home / "runner.pid", threading.Event()):
        assert main(args) == 1
        assert Config().get(key) == initial
        error = capsys.readouterr().err
        assert f"cannot change {key} while the runner is running" in error
        assert "nanodot stop" in error and "nanodot start" in error
        # Inspection remains available while policy mutation is blocked.
        assert main(["config", "list"]) == 0

    assert main(args) == 0
    if command == "unset":
        assert key not in Config().keys()
    else:
        expected = False if key == "os-notifications" else replacement
        assert Config().get(key) == expected


def test_runner_loads_policy_under_lock_before_ready(home: Path) -> None:
    from nanodot.cli import _wiring
    from nanodot.core.teardown import Teardown

    def checked_wiring(teardown=None):
        assert not (home / "runner.pid").exists()
        with pytest.raises(RunnerAlreadyRunning):
            with configuration_lock(home / "runner.pid"):
                pytest.fail("runner read configuration before acquiring ownership")
        return _wiring(teardown)

    with mock.patch("nanodot.cli._wiring", side_effect=checked_wiring) as wiring:
        assert main(["runner", "--once"]) == 0
    wiring.assert_called_once()
    (passed,), kwargs = wiring.call_args
    assert isinstance(passed, Teardown) and not kwargs


def test_background_start_does_not_report_ready_with_invalid_policy(home: Path, capsys) -> None:
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"github-auth-mode": "invalid"}))
    assert main(["start"]) == 1
    output = capsys.readouterr()
    assert "runner started" not in output.out
    assert "runner exited during startup" in output.err
    assert not (home / "runner.pid").exists()

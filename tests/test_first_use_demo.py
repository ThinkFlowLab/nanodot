"""Run the documented first-use example as a real CLI-process acceptance test."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_first_use_demo_offline(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "first-use"
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "first_pr_watch.py"), "--home", str(home)],
        cwd=root, env=dict(os.environ, PYTHONPATH=str(root / "src")),
        text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((home / "demo-report.json").read_text())
    assert len(report["scenarios"]) == 8
    assert all(row["passed"] for row in report["scenarios"])
    assert report["notification_kinds"] == ["checks-failed", "new-commit", "checks-passed"]
    assert report["runner_stopped"] is True
    assert report["credentials_used"] is False
    assert report["real_github_events"] is False


def test_demo_refuses_existing_data(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "existing"
    home.mkdir()
    (home / "keep.txt").write_text("untouched")
    result = subprocess.run(
        [sys.executable, str(root / "examples" / "first_pr_watch.py"), "--home", str(home)],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert "must be empty" in result.stderr
    assert (home / "keep.txt").read_text() == "untouched"


def _demo(tmp_path: Path):
    """Reuse the fake HTTP boundary, with each CLI command in a fresh process."""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("first_pr_watch", root / "examples" / "first_pr_watch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "cli-home"
    home.mkdir()
    demo = module.Demo(home)
    demo.cli("config", "set", "github-auth-mode", "anonymous")
    demo.cli("config", "set", "os-notifications", "false")
    # These are read-only watch behaviors: keep the write flow out of the
    # inbox assertions (auto is the installed default mode).
    demo.cli("config", "set", "permission-mode", "readonly")
    demo.cli("watch", "add", module.TARGET, "--yes", "--cadence", "300")
    return demo, demo.rows("tasks")[0]["id"]


# These tests exercise the actual crash boundary, not a caught Python error:
# NativeNotifier commits, then the child dies before TaskLoop saves its state.


@pytest.mark.parametrize("result,kind,final_state", [
    ("failure", "checks-failed", "active"),
    ("success", "checks-passed", "completed"),
])
def test_crash_replay_with_changed_optional_status(tmp_path, result, kind, final_state):
    demo, task_id = _demo(tmp_path)
    demo.set_fixture(result="pending", optional="pending")
    demo.cli("runner", "--once")
    before = json.loads(demo.rows("tasks")[0]["watch_state"])
    demo.wake(task_id)
    demo.set_fixture(result=result, optional="pending", crash_after_inbox=kind)
    demo.cli("runner", "--once", expected=86)
    assert len(demo.rows("inbox")) == 1
    delivered = demo.rows("inbox")[0]
    assert delivered["kind"] == kind
    assert demo.rows("tasks")[0]["state"] == "active"
    assert json.loads(demo.rows("tasks")[0]["watch_state"]) == before

    # Only unrelated evidence changes. The task/head/required outcome do not.
    demo.set_fixture(result=result, optional="success")
    demo.cli("runner", "--once")
    assert demo.rows("inbox") == [delivered]
    task = demo.rows("tasks")[0]
    assert task["state"] == final_state
    assert json.loads(task["watch_state"])["event_sequence"] == before.get("event_sequence", 0) + 1
    if final_state == "completed":
        assert task["next_check_at"] is None
    else:
        demo.wake(task_id)
    requests = demo.requests()
    demo.cli("runner", "--once")
    assert demo.rows("inbox") == [delivered]
    if final_state == "completed":
        assert demo.requests() == requests


@pytest.mark.parametrize("rules", ["empty", "hidden"])
def test_anonymous_cli_alerts_failures_without_known_required_checks(tmp_path, rules):
    demo, task_id = _demo(tmp_path)
    demo.set_fixture(rules=rules, result="pending")
    demo.cli("runner", "--once")
    assert demo.rows("inbox") == []
    demo.wake(task_id)
    demo.set_fixture(rules=rules, result="failure")
    demo.cli("runner", "--once")
    assert [row["kind"] for row in demo.rows("inbox")] == ["checks-failed"]
    assert demo.rows("tasks")[0]["state"] == "active"
    evidence = json.loads(demo.rows("inbox")[0]["evidence"])
    assert evidence["required_checks"] == ([] if rules == "empty" else None)
    demo.wake(task_id)
    demo.cli("runner", "--once")
    assert len(demo.rows("inbox")) == 1
    demo.wake(task_id)
    demo.set_fixture(rules=rules, result="success")
    demo.cli("runner", "--once")
    assert [row["kind"] for row in demo.rows("inbox")] == ["checks-failed"]
    assert demo.rows("tasks")[0]["state"] == "active"
    assert demo.rows("tasks")[0]["next_check_at"] is not None


def test_anonymous_cli_alerts_optional_failure_while_required_checks_pending(tmp_path):
    demo, task_id = _demo(tmp_path)
    demo.set_fixture(result="pending", optional="pending")
    demo.cli("runner", "--once")
    assert demo.rows("inbox") == []
    demo.wake(task_id)
    demo.set_fixture(result="pending", optional="failure")
    demo.cli("runner", "--once")
    delivered = demo.rows("inbox")[0]
    assert delivered["kind"] == "checks-failed"
    assert delivered["message"].endswith(": optional")
    assert demo.rows("tasks")[0]["state"] == "active"
    evidence = json.loads(delivered["evidence"])
    assert evidence["required_checks"] == [{"name": "test", "app_id": None}]
    demo.wake(task_id)
    demo.cli("runner", "--once")
    assert demo.rows("inbox") == [delivered]

    # Recovery of the optional result does not complete pending requirements.
    demo.wake(task_id)
    demo.set_fixture(result="pending", optional="success")
    demo.cli("runner", "--once")
    assert demo.rows("inbox") == [delivered]
    assert demo.rows("tasks")[0]["state"] == "active"
    demo.wake(task_id)
    demo.set_fixture(result="pending", optional="failure")
    demo.cli("runner", "--once")
    assert [row["kind"] for row in demo.rows("inbox")] == ["checks-failed", "checks-failed"]

    # Confirmed required success still wins over a failed optional check.
    demo.wake(task_id)
    demo.set_fixture(result="success", optional="failure")
    demo.cli("runner", "--once")
    assert [row["kind"] for row in demo.rows("inbox")] == [
        "checks-failed", "checks-failed", "checks-passed",
    ]
    task = demo.rows("tasks")[0]
    assert task["state"] == "completed" and task["next_check_at"] is None
    requests = demo.requests()
    demo.cli("runner", "--once")
    assert demo.requests() == requests
    assert len(demo.rows("inbox")) == 3

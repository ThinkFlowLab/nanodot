#!/usr/bin/env python3
"""Reproducible, credential-free CLI demo using fake HTTP and real persistence.

Run from a source checkout: python examples/first_pr_watch.py
Every CLI command executes in a fresh process. Only HTTP responses are faked;
CLI parsing/wiring, native GitHub parsing, runner, state machine and SQLite are
production code. A deliberate child-process exit exercises durable-inbox crash
recovery. This is a simulated PR lifecycle, not a real GitHub change event.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TARGET = "demo/nanodot#1"
SHA_A = "a" * 40
SHA_B = "b" * 40


def fake_cli(fixture_path: Path, argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from nanodot.cli import main
    from nanodot.native.notifier import NativeNotifier

    fixture = json.loads(fixture_path.read_text())
    head = fixture.get("head", SHA_A)
    state = fixture.get("result", "pending")
    calls_path = fixture_path.with_suffix(".calls.jsonl")

    def fake_http(request, **kwargs):
        assert request.get_method() == "GET", "demo must remain read-only"
        assert not request.has_header("Authorization"), "demo must remain anonymous"
        parsed = urlsplit(request.full_url)
        assert parsed.netloc == "api.github.com"
        path = parsed.path
        with calls_path.open("a") as handle:
            handle.write(json.dumps({"method": "GET", "path": path}) + "\n")
        if path == "/repos/demo/nanodot/pulls/1":
            response = {"head": {"sha": head}, "base": {"ref": "main"},
                        "state": "open", "merged": False}
        elif path == "/repos/demo/nanodot/branches/main":
            response = {"protected": True, "protection": {"required_status_checks": {
                "contexts": ["test"], "checks": [{"context": "test", "app_id": None}]}}}
        elif path == "/repos/demo/nanodot/rules/branches/main":
            response = []
        elif path.endswith("/check-suites"):
            response = {"total_count": 0, "check_suites": []}
        elif path.endswith("/status"):
            response = {"sha": SHA_A if fixture.get("stale") else head,
                        "total_count": 1, "statuses": [{"id": 10, "context": "test", "state": state}]}
        else:
            raise AssertionError(f"unexpected demo HTTP request: {path}")
        return io.BytesIO(json.dumps(response).encode())

    def deny_network(*args, **kwargs):
        raise AssertionError("the demo is offline; network access is forbidden")

    original_notify = NativeNotifier.notify

    def notify_then_crash(self, event):
        original_notify(self, event)
        if fixture.get("crash_after_inbox") and event.kind == "checks-failed":
            # A real process death after the durable inbox write, before the
            # task checkpoint. The next fresh runner must not duplicate it.
            os._exit(86)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("nanodot.native.github_client.authenticated_urlopen", fake_http))
        stack.enter_context(patch.object(NativeNotifier, "notify", notify_then_crash))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            stack.enter_context(patch.object(socket, name, deny_network))
        for name in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, name, deny_network))
        return main(argv)


class Demo:
    def __init__(self, home: Path):
        self.home = home
        self.fixture = home / "demo-http.json"
        self.calls = self.fixture.with_suffix(".calls.jsonl")
        self.transcript: list[str] = []
        self.results: list[dict] = []
        self.env = dict(os.environ, NANODOT_HOME=str(home), PYTHONPATH=str(ROOT / "src"))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            self.env[name] = "http://127.0.0.1:9"
        self.env.update(NO_PROXY="", no_proxy="")
        self.set_fixture()

    def set_fixture(self, **kwargs):
        self.fixture.write_text(json.dumps(kwargs))

    def cli(self, *args: str, expected: int = 0) -> str:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_cli", str(self.fixture), *args],
            env=self.env, text=True, capture_output=True, timeout=15,
        )
        output = process.stdout + process.stderr
        self.transcript.append(f"$ nanodot {' '.join(args)}\n{output}")
        if process.returncode != expected:
            raise AssertionError(f"{' '.join(args)}: expected exit {expected}, got {process.returncode}\n{output}")
        return output

    def wake(self, task_id: str):
        # Resume an already-active watch is idempotent. Pause/resume uses the
        # public CLI to request an immediate poll without editing its database.
        self.cli("watch", "pause", task_id)
        self.cli("watch", "resume", task_id)

    def rows(self, table: str) -> list[dict]:
        assert table in {"tasks", "inbox", "activity"}
        with sqlite3.connect(self.home / "nanodot.db") as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def requests(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def record(self, name: str):
        item = {"scenario": name, "passed": True, "inbox_count": len(self.rows("inbox")),
                "http_requests": self.requests()}
        self.results.append(item)
        print(f"PASS {name} (inbox={item['inbox_count']})", flush=True)

    def run(self) -> dict:
        self.cli("config", "set", "github-auth-mode", "anonymous")
        self.cli("config", "set", "os-notifications", "false")
        self.cli("watch", "add", TARGET, "--yes", "--cadence", "300")
        task_id = self.rows("tasks")[0]["id"]
        self.cli("watch", "show", task_id)
        self.cli("runner", "--once")
        assert self.rows("inbox") == []
        assert self.rows("tasks")[0]["state"] == "active"
        self.record("pending current-head CI stays quiet")

        self.set_fixture(result="failure", crash_after_inbox=True)
        self.wake(task_id)
        self.cli("runner", "--once", expected=86)
        assert len(self.rows("inbox")) == 1
        assert self.rows("tasks")[0]["state"] == "active"
        self.set_fixture(result="failure")
        self.cli("runner", "--once")
        self.wake(task_id)
        self.cli("runner", "--once")
        assert len(self.rows("inbox")) == 1
        self.record("hard crash and restart preserve exactly one failure inbox entry")

        self.set_fixture(head=SHA_B)
        self.wake(task_id)
        self.cli("runner", "--once")
        assert [row["kind"] for row in self.rows("inbox")] == ["checks-failed", "new-commit"]
        self.record("new commit invalidates the previous CI result")

        self.set_fixture(head=SHA_B, result="success", stale=True)
        self.wake(task_id)
        self.cli("runner", "--once")
        task = self.rows("tasks")[0]
        assert task["state"] == "active" and len(self.rows("inbox")) == 2
        assert json.loads(task["watch_state"])["consecutive_failures"] == 1
        self.record("stale-SHA success fails closed and schedules retry")

        self.set_fixture(head=SHA_B, result="success")
        self.wake(task_id)
        self.cli("runner", "--once")
        inbox = self.rows("inbox")
        assert [row["kind"] for row in inbox] == ["checks-failed", "new-commit", "checks-passed"]
        assert json.loads(inbox[-1]["evidence"])["head_sha"] == SHA_B
        assert self.rows("tasks")[0]["state"] == "completed"
        assert self.rows("tasks")[0]["next_check_at"] is None
        self.record("current-head success notifies once and completes the watch")

        count = self.requests()
        self.cli("runner", "--once")
        assert self.requests() == count and len(self.rows("inbox")) == 3
        self.record("completed watch survives process restart with zero future fetches")

        self.cli("watch", "add", TARGET, "--yes")
        second_id = next(row["id"] for row in self.rows("tasks") if row["id"] != task_id)
        self.cli("watch", "pause", second_id)
        self.cli("runner", "--once")
        assert self.requests() == count
        self.set_fixture()
        self.cli("watch", "resume", second_id)
        self.cli("runner", "--once")
        assert self.requests() > count
        self.cli("watch", "cancel", second_id)
        count = self.requests()
        self.cli("runner", "--once")
        assert self.requests() == count
        self.record("pause blocks reads, resume recovers, cancel excludes future work")

        # The production background daemon has no schedulable tasks here. It
        # starts without an HTTP override; dead proxies provide a second guard.
        try:
            self.cli("start")
            self.cli("status")
        finally:
            self.cli("stop")
        self.cli("status", expected=1)
        assert self.requests() == count and len(self.rows("inbox")) == 3
        self.record("real background runner starts and confirms cooperative shutdown")
        self.cli("inbox")
        self.cli("watch", "list")
        self.cli("activity", task_id)
        assert not (self.home / "secrets.json").exists()
        report = {"mode": "simulated GitHub HTTP, production CLI in fresh processes",
                  "real_github_events": False, "credentials_used": False,
                  "paid_models_used": False, "home": str(self.home),
                  "task_id": task_id, "scenarios": self.results,
                  "notification_kinds": [row["kind"] for row in self.rows("inbox")],
                  "runner_stopped": True}
        (self.home / "demo-report.json").write_text(json.dumps(report, indent=2) + "\n")
        (self.home / "demo-transcript.txt").write_text("\n".join(self.transcript))
        return report


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--_cli":
        return fake_cli(Path(sys.argv[2]), sys.argv[3:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="new or empty isolated demo data directory")
    args = parser.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix="nanodot-first-watch-"))
    home = home.resolve()
    if home.exists() and any(home.iterdir()):
        parser.error("--home must be empty so existing nanodot data is never changed")
    home.mkdir(parents=True, exist_ok=True)
    print("SIMULATED PR LIFECYCLE: no GitHub traffic, credentials, or model calls", flush=True)
    Demo(home).run()
    print(f"\n8 scenarios passed. Report and transcript: {home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

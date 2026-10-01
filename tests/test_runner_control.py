"""Process ownership never relies on or signals a pidfile's numeric PID."""

from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from nanodot.native.runner_control import (
    RunnerAlreadyRunning,
    RunnerControlError,
    RunnerLease,
    running_pid,
    startup_lock,
    stop_runner,
)


@pytest.mark.parametrize("contents", [
    "", "not a PID", "0", "-1", "1", str(os.getpid()),
    json.dumps({"pid": os.getpid(), "token": "a" * 64}),
    json.dumps({"pid": True, "token": "a" * 64}),
    "x" * 5000,
])
def test_stale_or_recycled_pid_is_never_signaled(tmp_path: Path, contents: str) -> None:
    pidfile = tmp_path / "runner.pid"
    pidfile.write_text(contents)
    with mock.patch("os.kill", side_effect=AssertionError("must not signal a PID")):
        assert running_pid(pidfile) is None
        assert stop_runner(pidfile) is False
    assert not pidfile.exists()


def test_lease_publishes_private_identity_and_cleans_up(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    with RunnerLease(pidfile, threading.Event()):
        record = json.loads(pidfile.read_text())
        assert record["pid"] == os.getpid()
        assert len(record["token"]) == 64
        assert stat.S_IMODE(pidfile.stat().st_mode) == 0o600
        assert running_pid(pidfile) == os.getpid()
    assert running_pid(pidfile) is None
    assert not pidfile.exists()
    # Never unlink a lock's inode: another command may already have it open.
    assert pidfile.with_suffix(".lock").exists()


def test_second_lease_cannot_replace_the_first_record(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    with RunnerLease(pidfile, threading.Event()):
        original = pidfile.read_text()
        with pytest.raises(RunnerAlreadyRunning):
            with RunnerLease(pidfile, threading.Event()):
                pytest.fail("second runner acquired ownership")
        assert pidfile.read_text() == original


def test_simultaneous_leases_have_exactly_one_winner(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    gate = threading.Barrier(3)
    release = threading.Event()
    results: queue.Queue[str] = queue.Queue()

    def contend() -> None:
        gate.wait()
        try:
            with RunnerLease(pidfile, threading.Event()):
                results.put("owner")
                release.wait(5)
        except RunnerAlreadyRunning:
            results.put("rejected")

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        gate.wait()
        assert sorted([results.get(timeout=5), results.get(timeout=5)]) == [
            "owner", "rejected",
        ]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert running_pid(pidfile) is None


def test_stop_timeout_keeps_ownership_and_pending_request(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    stop = threading.Event()
    with RunnerLease(pidfile, stop):
        original = pidfile.read_text()
        with pytest.raises(RunnerControlError, match="stop is still pending"):
            stop_runner(pidfile, timeout=0.15)
        assert stop.wait(1)
        assert pidfile.read_text() == original
        assert pidfile.with_suffix(".stop").exists()
        assert running_pid(pidfile) == os.getpid()
    assert not pidfile.exists()
    assert not pidfile.with_suffix(".stop").exists()


def test_wrong_token_cannot_stop_a_live_runner(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    stop = threading.Event()
    with RunnerLease(pidfile, stop):
        pidfile.with_suffix(".stop").write_text(json.dumps({
            "pid": os.getpid(), "token": "0" * 64,
        }))
        assert not stop.wait(0.15)


def test_previous_runner_stop_request_cannot_stop_replacement(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    with RunnerLease(pidfile, threading.Event()):
        previous = pidfile.read_text()
    stop = threading.Event()
    with RunnerLease(pidfile, stop):
        assert pidfile.read_text() != previous
        pidfile.with_suffix(".stop").write_text(previous)
        assert not stop.wait(0.15)


def test_corrupt_live_identity_fails_closed(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    stop = threading.Event()
    with RunnerLease(pidfile, stop):
        pidfile.write_text(str(os.getpid()))
        with pytest.raises(RunnerControlError, match="metadata is unavailable"):
            running_pid(pidfile)
        with pytest.raises(RunnerControlError, match="identity is unavailable"):
            stop_runner(pidfile)
        assert not stop.is_set()
        with pytest.raises(RunnerAlreadyRunning):
            with RunnerLease(pidfile, threading.Event()):
                pytest.fail("corrupt metadata bypassed the lifetime lock")


def test_changed_identity_is_not_reported_as_stopped(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    stop = threading.Event()
    with RunnerLease(pidfile, stop):
        def replace_identity() -> None:
            assert stop.wait(5)
            record = json.loads(pidfile.read_text())
            record["token"] = "f" * 64
            pidfile.write_text(json.dumps(record))

        replacer = threading.Thread(target=replace_identity)
        replacer.start()
        try:
            with pytest.raises(RunnerControlError, match="identity changed"):
                stop_runner(pidfile, timeout=5)
            assert running_pid(pidfile) == os.getpid()
        finally:
            replacer.join(timeout=5)


def test_lease_exception_releases_ownership(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    with pytest.raises(RuntimeError, match="scheduler failed"):
        with RunnerLease(pidfile, threading.Event()):
            raise RuntimeError("scheduler failed")
    assert running_pid(pidfile) is None
    with RunnerLease(pidfile, threading.Event()):
        assert running_pid(pidfile) == os.getpid()


def test_startup_lock_serializes_and_has_a_deadline(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    with startup_lock(pidfile):
        with pytest.raises(RunnerControlError, match="start/stop command"):
            with startup_lock(pidfile, timeout=0.01):
                pytest.fail("two startup commands acquired ownership")
    with startup_lock(pidfile):
        pass


def test_stop_waits_for_a_real_process_to_finish_scheduler(tmp_path: Path) -> None:
    pidfile = tmp_path / "runner.pid"
    finished = tmp_path / "finished"
    script = """
import sys, threading, time
from pathlib import Path
from nanodot.native.runner_control import RunnerLease
stop = threading.Event()
with RunnerLease(Path(sys.argv[1]), stop):
    print('ready', flush=True)
    stop.wait(5)
    time.sleep(0.15)
    Path(sys.argv[2]).write_text('scheduler stopped')
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(pidfile), str(finished)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if running_pid(pidfile) == process.pid:
                    break
            except RunnerControlError:
                pass
            assert process.poll() is None
            time.sleep(0.01)
        else:
            pytest.fail("child runner did not publish its lease")
        assert not finished.exists()
        with mock.patch("os.kill", side_effect=AssertionError("must not signal a PID")):
            assert stop_runner(pidfile, timeout=5) is True
        assert finished.read_text() == "scheduler stopped"
        assert running_pid(pidfile) is None
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)

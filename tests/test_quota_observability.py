"""Quota observability: the one performance fact the watcher consumes.

X-RateLimit-Remaining rides on every fetch (tightest seen), lands in
each poll's observation evidence, and never leaks into snapshot identity
— fingerprints ignore it, so dedup and notifications cannot feel it.
"""

from __future__ import annotations

import io
import time
import urllib.error
import urllib.request
from pathlib import Path
from email.message import Message

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeSink
from urllib.response import addinfourl

import nanodot.native.github_client as client_module
from nanodot.core.activity import ActivityLog
from nanodot.core.runner import CHECK_OBSERVED, RunOutcome, TaskLoop
from nanodot.core import statemachine
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.native.github_client import GitHubSnapshotFetcher


def _response(body: bytes, remaining: str | None = None) -> addinfourl:
    headers = Message()
    headers["Content-Type"] = "application/json"
    if remaining is not None:
        headers["X-RateLimit-Remaining"] = remaining
    result = addinfourl(io.BytesIO(body), headers, "https://api.github.test", 200)
    result.msg = "OK"
    return result


def test_fetch_records_the_tightest_quota_seen(home: Path, monkeypatch) -> None:
    """Multiple requests in one fetch: the minimum remaining wins."""
    # One open PR, one passing check, no required metadata: the smallest
    # realistic fetch (pull, branch, suites, statuses, re-read).
    pages = {
        "/repos/o/r/pulls/1": (
            b'{"state": "open", "merged": false, '
            b'"head": {"sha": "s1"}, "base": {"ref": "main"}}'
        ),
        "/repos/o/r/branches/main": b'{"protected": false}',
        "/repos/o/r/commits/s1/check-suites": (
            b'{"total_count": 1, "check_suites": '
            b'[{"id": 7, "head_sha": "s1", "app": {"id": 1, "slug": "ci"}, '
            b'"status": "completed"}]}'
        ),
        "/repos/o/r/check-suites/7/check-runs": (
            b'{"total_count": 1, "check_runs": '
            b'[{"id": 9, "name": "ci", "status": "completed", '
            b'"conclusion": "success", "head_sha": "s1", '
            b'"app": {"id": 1}, "check_suite": {"id": 7}}]}'
        ),
        "/repos/o/r/commits/s1/status": (
            b'{"sha": "s1", "total_count": 0, "statuses": []}'
        ),
        "/repos/o/r/rules/branches/main": b'[]',
    }
    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        path = request.full_url.split("api.github.com", 1)[1].split("?")[0]
        if path not in pages:
            raise urllib.error.HTTPError(
                request.full_url, 404, "x", {}, io.BytesIO(b"{}")
            )
        calls["n"] += 1
        # Early calls report headroom; every later one the tighter 4980.
        remaining = "4999" if calls["n"] <= 3 else "4980"
        return _response(pages[path], remaining)

    monkeypatch.setattr(client_module, "authenticated_urlopen", fake_urlopen)
    fetcher = GitHubSnapshotFetcher(token="t")
    snapshot = fetcher.fetch(PRTarget.parse("o/r#1"))
    assert calls["n"] >= 5  # a real fetch spans several requests
    assert snapshot.rate_limit_remaining == 4980  # the minimum seen


def test_missing_or_malformed_quota_header_is_ignored(home: Path, monkeypatch) -> None:
    def fake_urlopen(request, timeout=None):
        path = request.full_url.split("api.github.com", 1)[1].split("?")[0]
        if path == "/repos/o/r/pulls/1":
            return _response(
                b'{"state": "closed", "merged": true, '
                b'"head": {"sha": "s1"}, "base": {"ref": "main"}}',
                remaining="not-a-number",
            )
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client_module, "authenticated_urlopen", fake_urlopen)
    fetcher = GitHubSnapshotFetcher(token="t")
    snapshot = fetcher.fetch(PRTarget.parse("o/r#1"))
    assert snapshot.pr_state == "merged"
    assert snapshot.rate_limit_remaining is None


def test_quota_never_changes_snapshot_identity() -> None:
    """Only-quota differences must not re-fire events: dedup keys on the
    fingerprint, which excludes advisory transport metadata."""
    fake = FakeGitHub(PRTarget.parse("o/r#1"))
    fake.set_pr("open", head_sha="s1")
    fake.add_check("ci", FAILURE, sha="s1")
    task = Task(target=fake.target, purpose="watch")
    fake.rate_limit_remaining = 5000
    task.watch_state, events = statemachine.step(task, fake.snapshot(), now=1000.0)
    assert [e.kind for e in events] == [statemachine.CHECKS_FAILED]

    fake.rate_limit_remaining = 4999  # a new poll, quota decremented
    task.watch_state, events = statemachine.step(task, fake.snapshot(), now=1300.0)
    assert events == []  # unchanged PR state: nothing new


def test_observation_evidence_carries_quota(home: Path) -> None:
    fake = FakeGitHub(PRTarget.parse("o/r#1"))
    fake.set_pr("open", head_sha="s1")
    fake.add_check("ci", None, sha="s1", status="queued")
    fake.rate_limit_remaining = 4321
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    loop = TaskLoop(store, fake, FakeSink(), activity)
    task = store.create(Task(target=fake.target, purpose="watch", next_check_at=0.0))
    assert loop.run_once(task, 1000.0) is RunOutcome.OK
    observed = activity.query(task_id=task.id, kinds=(CHECK_OBSERVED,))
    assert observed[0].evidence["rate_limit_remaining"] == 4321


# -- offline loop-cost baseline ------------------------------------------------


def test_loop_cost_baseline_100_tasks(home: Path) -> None:
    """A regression bound, not a benchmark: one scheduler pass over 100
    watches (fakes, real SQLite) must stay well inside interactive time.
    The generous constant catches structural regressions (per-task O(n²)
    work, fsync storms), not hardware noise."""
    target = PRTarget.parse("o/r#1")
    fake = FakeGitHub(target)
    fake.set_pr("open", head_sha="s1")
    fake.add_check("ci", None, sha="s1", status="queued")
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    loop = TaskLoop(store, fake, FakeSink(), activity)
    for index in range(100):
        store.create(
            Task(target=target, purpose=f"watch {index}", next_check_at=0.0)
        )
    clock = FakeClock()

    start = time.monotonic()
    for task in store.list_schedulable(clock.time()):
        loop.run_once(task, clock.time())
    elapsed = time.monotonic() - start

    assert elapsed < 10.0, f"100-task scheduler pass took {elapsed:.1f}s"

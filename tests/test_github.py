"""GitHub snapshot port acceptance tests (issue #6)."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from urllib.error import HTTPError

import pytest

from fakes import COMPLETED, FAILURE, QUEUED, SUCCESS, TYPICAL_ERRORS, FakeGitHub

from nanodot.core.github_eval import (
    CheckOutcome,
    checks_passing_on_current_commit,
    evaluate_checks,
)
from nanodot.core.tasks import PRTarget
from nanodot.native.github_client import GitHubSnapshotFetcher
from nanodot.ports.github import (
    AuthLostError,
    PRNotFoundError,
    RetryableError,
)

TARGET = PRTarget.parse("thinkflowlab/nanodot#7")


# -- pure evaluation: the commit-pinning rule ------------------------------


def snapshot(fake: FakeGitHub):
    return fake.snapshot()


def test_old_sha_passing_results_cannot_satisfy_new_head() -> None:
    fake = FakeGitHub(TARGET)
    fake.set_pr("open", head_sha="sha-2")
    fake.add_check("ci", SUCCESS, sha="sha-1")  # passed on the OLD commit
    fake.add_check("ci", None, sha="sha-2", status=QUEUED)

    snap = fake.snapshot()
    # The fake deliberately mixes old-SHA runs into the snapshot payload;
    # evaluation must ignore everything not keyed to the current head.
    assert any(run.sha == "sha-1" for run in snap.checks)
    assert evaluate_checks(snap) is CheckOutcome.PENDING
    assert not checks_passing_on_current_commit(snap)

    # The new commit's check completes successfully — now, and only now,
    # the watch can be satisfied.
    fake.checks["sha-2"] = []
    fake.add_check("ci", SUCCESS, sha="sha-2")
    snap2 = fake.snapshot()
    assert evaluate_checks(snap2) is CheckOutcome.PASSING
    assert checks_passing_on_current_commit(snap2)


def test_outcomes_distinguished() -> None:
    def outcome(checks, sha="s1", state="open"):
        fake = FakeGitHub(TARGET)
        fake.set_pr(state, head_sha=sha)
        for check in checks:
            fake.add_check(*check[:2], sha=check[2] if len(check) > 2 else sha,
                           status=check[3] if len(check) > 3 else COMPLETED)
        return evaluate_checks(fake.snapshot())

    assert outcome([("ci", SUCCESS)]) is CheckOutcome.PASSING
    assert outcome([("ci", FAILURE)]) is CheckOutcome.FAILING
    assert outcome([("ci", None, "s1", QUEUED), ("lint", SUCCESS)]) is CheckOutcome.PENDING
    assert outcome([]) is CheckOutcome.NO_CHECKS
    # skipped/neutral alone is not a confirmed pass
    assert outcome([("ci", "skipped")]) is CheckOutcome.PENDING


# -- native client: error mapping, read-only, no partial success -----------


class _FakeHTTPResponse(io.BytesIO):
    def __init__(self, body: dict, code: int = 200, headers: dict | None = None) -> None:
        super().__init__(json.dumps(body).encode())
        self.code = code
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _patch_urlopen(monkeypatch, handler) -> list:
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr("nanodot.native.github_client.urllib.request.urlopen", fake_urlopen)
    return calls


def _pull_body(sha: str = "abc123", state: str = "open", merged: bool = False) -> dict:
    return {"head": {"sha": sha}, "state": state, "merged": merged}


def _check_runs(*runs: tuple) -> dict:
    return {
        "check_runs": [
            {"name": name, "status": status, "conclusion": conclusion}
            for name, status, conclusion in runs
        ]
    }


def test_client_maps_error_codes(monkeypatch) -> None:
    cases = [
        (401, AuthLostError),
        (403, AuthLostError),
        (403, RetryableError, {"X-RateLimit-Remaining": "0"}),
        (429, RetryableError),
        (500, RetryableError),
        (404, PRNotFoundError),
    ]
    for case in cases:
        code, expected = case[0], case[1]
        headers = case[2] if len(case) > 2 else {}

        def handler(request, code=code, headers=headers):
            raise HTTPError(
                request.full_url, code, "boom", headers, io.BytesIO(b"{}")
            )

        _patch_urlopen(monkeypatch, handler)
        fetcher = GitHubSnapshotFetcher(token="tok")
        with pytest.raises(expected):
            fetcher.fetch(TARGET)


def test_client_builds_commit_pinned_snapshot(monkeypatch) -> None:
    def handler(request):
        if "/pulls/" in request.full_url:
            return _FakeHTTPResponse(_pull_body(sha="abc123"))
        return _FakeHTTPResponse(_check_runs(("ci", COMPLETED, SUCCESS)))

    calls = _patch_urlopen(monkeypatch, handler)
    fetcher = GitHubSnapshotFetcher(token="tok")
    snap = fetcher.fetch(TARGET)
    assert snap.head_sha == "abc123"
    assert all(run.sha == "abc123" for run in snap.checks)
    assert snap.pr_state == "open"
    assert snap.url.endswith("/pull/7")
    assert all(request.get_method() == "GET" for request in calls)


def test_no_partial_snapshot_on_second_call_failure(monkeypatch) -> None:
    state = {"calls": 0}

    def handler(request):
        state["calls"] += 1
        if "/pulls/" in request.full_url:
            return _FakeHTTPResponse(_pull_body(sha="abc123"))
        raise HTTPError(request.full_url, 502, "bad gateway", {}, io.BytesIO(b"{}"))

    _patch_urlopen(monkeypatch, handler)
    fetcher = GitHubSnapshotFetcher(token="tok")
    with pytest.raises(RetryableError):
        fetcher.fetch(TARGET)
    assert state["calls"] == 2  # it attempted the check-runs call and failed


def test_missing_token_is_auth_blocker(monkeypatch, home: Path) -> None:
    fetcher = GitHubSnapshotFetcher()
    with pytest.raises(AuthLostError, match="no GitHub token"):
        fetcher.fetch(TARGET)


def test_fake_github_reproduces_error_scenarios() -> None:
    fake = FakeGitHub(TARGET)
    assert fake.fetch(TARGET).pr_state == "open"
    for error in TYPICAL_ERRORS.values():
        fake.fail_with(error)
        with pytest.raises(type(error)):
            fake.fetch(TARGET)
    fake.fail_with(None)
    fake.set_pr("merged", head_sha="sha-9")
    assert fake.fetch(TARGET).pr_state == "merged"

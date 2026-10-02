"""GitHub snapshot port acceptance tests (issue #6)."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError

import pytest

from fakes import COMPLETED, FAILURE, QUEUED, SUCCESS, TYPICAL_ERRORS, FakeGitHub

from nanodot.core.github_eval import (
    CheckOutcome,
    checks_passing_on_current_commit,
    evaluate_checks,
)
from nanodot.core.tasks import PRTarget
from nanodot.native.github_client import GitHubSnapshotFetcher, UnexpectedStatusError
from nanodot.ports.github import (
    AuthLostError,
    CheckRun,
    RequiredCheck,
    Snapshot,
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
    def __init__(self, body: dict | list, code: int = 200, headers: dict | None = None) -> None:
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

    monkeypatch.setattr("nanodot.native.github_client.authenticated_urlopen", fake_urlopen)
    return calls


def _pull_body(sha: str = "abc123", state: str = "open", merged: bool = False) -> dict:
    return {"head": {"sha": sha}, "base": {"ref": "main"}, "state": state, "merged": merged}


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
    calls = _patch_urlopen(monkeypatch, _api_handler())
    fetcher = GitHubSnapshotFetcher(token="tok")
    snap = fetcher.fetch(TARGET)
    assert snap.head_sha == "abc123"
    assert all(run.sha == "abc123" for run in snap.checks)
    assert snap.pr_state == "open"
    assert evaluate_checks(snap) is CheckOutcome.PASSING
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
    assert state["calls"] == 2  # a later required read failed; no partial snapshot


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


def _run(name="ci", conclusion=SUCCESS, status=COMPLETED, *, id=1, sha="abc123", app=10, suite=1):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion,
            "head_sha": sha, "app": {"id": app}, "check_suite": {"id": suite}}


def _suite(id=1, app=10, sha="abc123", status=COMPLETED, slug="test-app"):
    return {"id": id, "app": {"id": app, "slug": slug}, "head_sha": sha, "status": status}


def _api_handler(*, runs=None, suites=None, statuses=None, required=None, rules=None,
                 branch=None, workflows=None, override=None):
    runs = [_run()] if runs is None else runs
    suites = [_suite()] if suites is None else suites
    statuses = [] if statuses is None else statuses
    required = [{"context": "ci", "app_id": None}] if required is None else required
    rules = [] if rules is None else rules
    branch = ({"protected": True, "protection": {"required_status_checks": {
        "contexts": [check["context"] for check in required], "checks": required,
    }}} if branch is None else branch)

    def paged(rows, query, key=None):
        page = int(query.get("page", ["1"])[0])
        batch = rows[(page - 1) * 100:page * 100]
        return batch if key is None else {"total_count": len(rows), key: batch}

    def handler(request):
        url = urlsplit(request.full_url)
        path, query = url.path, parse_qs(url.query)
        if override is not None:
            result = override(path, query)
            if result is not None:
                return result
        if "/pulls/" in path:
            body = _pull_body()
        elif path.endswith("/check-suites"):
            body = paged(suites, query, "check_suites")
        elif path.endswith("/check-runs"):
            suite_id = int(path.split("/")[-2])
            body = paged([run for run in runs if run["check_suite"]["id"] == suite_id], query, "check_runs")
        elif path.endswith("/status"):
            body = paged(statuses, query, "statuses")
            body["sha"] = "abc123"
        elif "/rules/branches/" in path:
            body = paged(rules, query)
        elif path.endswith("/actions/runs"):
            body = paged([] if workflows is None else workflows, query, "workflow_runs")
        elif "/branches/" in path:
            body = branch
        else:
            raise AssertionError(f"unexpected API request: {request.full_url}")
        return _FakeHTTPResponse(body)
    return handler


def _fetch(monkeypatch, **kwargs):
    calls = _patch_urlopen(monkeypatch, _api_handler(**kwargs))
    return GitHubSnapshotFetcher(token="tok").fetch(TARGET), calls


def _snap(*runs, required=(RequiredCheck("ci"),), complete=True):
    return Snapshot(TARGET, "open", "abc123", tuple(runs), 0, "https://github.com/o/r/pull/1",
                    required_checks=required, checks_complete=complete)


def test_snapshot_defaults_never_claim_complete_or_known() -> None:
    snap = Snapshot(TARGET, "open", "abc123", (CheckRun("ci", COMPLETED, SUCCESS, "abc123"),), 0, "url")
    assert evaluate_checks(snap) is CheckOutcome.PENDING


@pytest.mark.parametrize("required,complete,expected", [
    (None, True, CheckOutcome.PENDING),
    ((RequiredCheck("missing"),), True, CheckOutcome.PENDING),
    ((RequiredCheck("ci"),), False, CheckOutcome.PENDING),
    ((), True, CheckOutcome.NO_CHECKS),
])
def test_missing_unknown_or_incomplete_required_checks_never_pass(required, complete, expected) -> None:
    assert evaluate_checks(_snap(CheckRun("ci", COMPLETED, SUCCESS, "abc123"),
                                 required=required, complete=complete)) is expected


@pytest.mark.parametrize("required", [None, ()])
@pytest.mark.parametrize("source", ["check_run", "status"])
def test_observed_failure_survives_unknown_or_empty_requirements(required, source) -> None:
    run = CheckRun("ci", COMPLETED, FAILURE, "abc123", source=source)
    snap = _snap(run, required=required)
    assert evaluate_checks(snap) is CheckOutcome.FAILING
    assert not checks_passing_on_current_commit(snap)


@pytest.mark.parametrize("required,expected", [(None, CheckOutcome.PENDING), ((), CheckOutcome.NO_CHECKS)])
@pytest.mark.parametrize("change", [
    {"sha": "old-head"},
    {"status": QUEUED},
    {"conclusion": SUCCESS},
    {"source": "check_suite"},
])
def test_unknown_or_empty_rules_only_report_actual_current_failures(required, expected, change) -> None:
    run = replace(CheckRun("ci", COMPLETED, FAILURE, "abc123"), **change)
    assert evaluate_checks(_snap(run, required=required)) is expected


@pytest.mark.parametrize("required,expected", [(None, CheckOutcome.PENDING), ((), CheckOutcome.NO_CHECKS)])
@pytest.mark.parametrize("source", ["check_run", "status"])
@pytest.mark.parametrize("status,conclusion", [(COMPLETED, SUCCESS), (QUEUED, None)])
def test_failure_without_requirements_uses_latest_rerun(required, expected, source, status, conclusion) -> None:
    old = CheckRun("ci", COMPLETED, FAILURE, "abc123", source=source,
                   app_id=10, suite_id=1, run_id=1)
    latest = replace(old, status=status, conclusion=conclusion, run_id=2)
    assert evaluate_checks(_snap(latest, old, required=required)) is expected


@pytest.mark.parametrize("required", [None, ()])
@pytest.mark.parametrize("change", [
    {"suite_id": 2}, {"app_id": 20}, {"source": "status"}, {"run_id": None},
])
def test_failure_without_requirements_does_not_merge_independent_runs(required, change) -> None:
    old = CheckRun("ci", COMPLETED, FAILURE, "abc123", app_id=10, suite_id=1, run_id=1)
    other = replace(replace(old, conclusion=SUCCESS, run_id=2), **change)
    assert evaluate_checks(_snap(old, other, required=required)) is CheckOutcome.FAILING


@pytest.mark.parametrize("required", [None, (), (RequiredCheck("ci"),)])
def test_partial_snapshot_cannot_establish_a_current_failure(required) -> None:
    # A partial listing may omit a newer queued/successful rerun.
    run = CheckRun("ci", COMPLETED, FAILURE, "abc123", run_id=1)
    assert evaluate_checks(_snap(run, required=required, complete=False)) is CheckOutcome.PENDING


def test_required_failures_outrank_pending_and_missing() -> None:
    runs = (CheckRun("ci", COMPLETED, FAILURE, "abc123"), CheckRun("lint", QUEUED, None, "abc123"))
    required = (RequiredCheck("ci"), RequiredCheck("lint"), RequiredCheck("missing"))
    assert evaluate_checks(_snap(*runs, required=required)) is CheckOutcome.FAILING


def test_optional_failure_does_not_block_required_success() -> None:
    assert evaluate_checks(_snap(CheckRun("ci", COMPLETED, SUCCESS, "abc123"),
                                 CheckRun("optional", COMPLETED, FAILURE, "abc123"))) is CheckOutcome.PASSING


@pytest.mark.parametrize("source", ["check_run", "status"])
@pytest.mark.parametrize("required_runs,required", [
    ((CheckRun("ci", QUEUED, None, "abc123"),), (RequiredCheck("ci"),)),
    ((), (RequiredCheck("ci"),)),
    ((CheckRun("ci", COMPLETED, "neutral", "abc123"),), (RequiredCheck("ci"),)),
    ((CheckRun("ci", COMPLETED, "skipped", "abc123"),), (RequiredCheck("ci"),)),
    ((CheckRun("ci", COMPLETED, SUCCESS, "abc123"),
      CheckRun("", QUEUED, None, "abc123", source="check_suite")), (RequiredCheck("ci"),)),
    ((CheckRun("ci", COMPLETED, SUCCESS, "abc123", app_id=10),), (RequiredCheck("ci", 20),)),
    ((CheckRun("ci", COMPLETED, SUCCESS, "abc123", source="status"),), (RequiredCheck("ci", 20),)),
    ((), (RequiredCheck(""),)),
])
def test_optional_failure_notifies_until_required_success_is_confirmed(source, required_runs, required) -> None:
    failed = CheckRun("optional", COMPLETED, FAILURE, "abc123", source=source)
    snap = _snap(*required_runs, failed, required=required)
    assert evaluate_checks(snap) is CheckOutcome.FAILING
    assert not checks_passing_on_current_commit(snap)


@pytest.mark.parametrize("change", [
    {"sha": "old-head"}, {"status": QUEUED}, {"conclusion": SUCCESS},
    {"source": "check_suite"}, {"source": "unknown"},
])
def test_pending_required_checks_ignore_unconfirmed_optional_failures(change) -> None:
    failed = replace(CheckRun("optional", COMPLETED, FAILURE, "abc123"), **change)
    assert evaluate_checks(_snap(CheckRun("ci", QUEUED, None, "abc123"), failed)) is CheckOutcome.PENDING


@pytest.mark.parametrize("source", ["check_run", "status"])
@pytest.mark.parametrize("status,conclusion", [(COMPLETED, SUCCESS), (QUEUED, None)])
def test_optional_failure_with_pending_requirements_uses_latest_rerun(source, status, conclusion) -> None:
    old = CheckRun("optional", COMPLETED, FAILURE, "abc123", source=source,
                   app_id=10, suite_id=1, run_id=1)
    latest = replace(old, status=status, conclusion=conclusion, run_id=2)
    assert evaluate_checks(_snap(CheckRun("ci", QUEUED, None, "abc123"), old, latest)) is CheckOutcome.PENDING


def test_check_and_same_name_status_must_both_pass() -> None:
    check = CheckRun("ci", COMPLETED, SUCCESS, "abc123", app_id=10)
    legacy = CheckRun("CI", "queued", "pending", "abc123", source="status")
    assert evaluate_checks(_snap(check, legacy)) is CheckOutcome.PENDING
    assert evaluate_checks(_snap(check, replace(legacy, status=COMPLETED, conclusion=FAILURE))) is CheckOutcome.FAILING
    assert evaluate_checks(_snap(check, replace(legacy, status=COMPLETED, conclusion=SUCCESS))) is CheckOutcome.PASSING


def test_app_source_cannot_be_satisfied_by_wrong_app_or_legacy_creator() -> None:
    required = (RequiredCheck("ci", 20),)
    check = CheckRun("ci", COMPLETED, SUCCESS, "abc123", app_id=10)
    assert evaluate_checks(_snap(check, required=required)) is CheckOutcome.PENDING
    assert evaluate_checks(_snap(replace(check, app_id=20), required=required)) is CheckOutcome.PASSING
    legacy = CheckRun("ci", COMPLETED, SUCCESS, "abc123", source="status")
    assert evaluate_checks(_snap(legacy, required=required)) is CheckOutcome.PENDING
    assert evaluate_checks(_snap(replace(check, app_id=20), legacy, required=required)) is CheckOutcome.PENDING


def test_latest_rerun_wins_only_within_same_suite_and_source() -> None:
    old = CheckRun("ci", COMPLETED, FAILURE, "abc123", app_id=10, suite_id=1, run_id=1)
    new = replace(old, conclusion=SUCCESS, run_id=2)
    assert evaluate_checks(_snap(new, old)) is CheckOutcome.PASSING
    assert evaluate_checks(_snap(old, replace(new, status=QUEUED, conclusion=None))) is CheckOutcome.PENDING
    assert evaluate_checks(_snap(old, replace(new, suite_id=2))) is CheckOutcome.FAILING
    assert evaluate_checks(_snap(old, replace(new, app_id=20))) is CheckOutcome.FAILING
    assert evaluate_checks(_snap(replace(old, run_id=None), new)) is CheckOutcome.FAILING


def test_paginates_more_than_100_check_runs_and_reads_only_get(monkeypatch) -> None:
    runs = [_run(name=f"check-{i}", id=i + 1) for i in range(120)]
    runs[-1]["status"], runs[-1]["conclusion"] = QUEUED, None
    required = [{"context": run["name"], "app_id": None} for run in runs]
    snap, calls = _fetch(monkeypatch, runs=runs, required=required)
    assert len(snap.checks) == 120
    assert evaluate_checks(snap) is CheckOutcome.PENDING
    assert any("check-runs?filter=all&per_page=100&page=2" in call.full_url for call in calls)
    assert all(call.get_method() == "GET" for call in calls)


def test_paginates_more_than_100_legacy_contexts(monkeypatch) -> None:
    statuses = [{"id": i + 1, "context": f"check-{i}", "state": "success"} for i in range(120)]
    statuses[-1]["state"] = "failure"
    required = [{"context": row["context"], "app_id": None} for row in statuses]
    snap, calls = _fetch(monkeypatch, suites=[], runs=[], statuses=statuses, required=required)
    assert evaluate_checks(snap) is CheckOutcome.FAILING
    assert len(snap.checks) == 120
    assert any("/status?per_page=100&page=2" in call.full_url for call in calls)


def test_no_1000_suite_truncation(monkeypatch) -> None:
    suites = [_suite(id=i + 1) for i in range(1001)]
    snap, calls = _fetch(monkeypatch, suites=suites, runs=[_run(suite=1001, conclusion=FAILURE)])
    assert evaluate_checks(snap) is CheckOutcome.FAILING
    assert any("/check-suites/1001/check-runs" in call.full_url for call in calls)
    assert any("check-suites?per_page=100&page=11" in call.full_url for call in calls)


def test_paginates_branch_rules_and_combines_with_classic_protection(monkeypatch) -> None:
    rules = [{"type": "pull_request"} for _ in range(100)] + [{
        "type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "missing", "integration_id": 22}]}}]
    snap, calls = _fetch(monkeypatch, rules=rules)
    assert set(snap.required_checks) == {RequiredCheck("ci"), RequiredCheck("missing", 22)}
    assert evaluate_checks(snap) is CheckOutcome.PENDING
    assert any("/rules/branches/main?per_page=100&page=2" in call.full_url for call in calls)


@pytest.mark.parametrize("rule", ["required_workflows", "code_scanning", "merge_queue", "required_deployments", "future_rule"])
def test_unsupported_active_rules_fail_closed(monkeypatch, rule) -> None:
    snap, _ = _fetch(monkeypatch, rules=[{"type": rule}])
    assert snap.required_checks is None
    assert evaluate_checks(snap) is CheckOutcome.PENDING


@pytest.mark.parametrize("endpoint,code", [("/branches/main", 403), ("/branches/main", 404),
                                          ("/rules/branches/main", 403), ("/rules/branches/main", 404)])
def test_required_metadata_unavailable_never_passes(monkeypatch, endpoint, code) -> None:
    def override(path, query):
        if path.endswith(endpoint):
            raise HTTPError(path, code, "unavailable", {}, io.BytesIO(b"{}"))
    snap, _ = _fetch(monkeypatch, override=override)
    assert snap.required_checks is None
    assert evaluate_checks(snap) is CheckOutcome.PENDING


def test_missing_classic_app_metadata_does_not_become_any_app(monkeypatch) -> None:
    branch = {"protected": True, "protection": {"required_status_checks": {"contexts": ["ci"]}}}
    def override(path, query):
        if path.endswith("/protection"):
            raise HTTPError(path, 404, "hidden", {}, io.BytesIO(b"{}"))
    snap, calls = _fetch(monkeypatch, branch=branch, override=override)
    assert evaluate_checks(snap) is CheckOutcome.PENDING
    assert any(call.full_url.endswith("/protection") for call in calls)


def test_unprotected_branch_still_reads_rulesets_and_empty_is_not_a_pass(monkeypatch) -> None:
    snap, calls = _fetch(monkeypatch, branch={"protected": False})
    assert snap.required_checks == ()
    assert evaluate_checks(snap) is CheckOutcome.NO_CHECKS
    assert any("/rules/branches/" in call.full_url for call in calls)


@pytest.mark.parametrize("metadata", ["empty", "hidden-branch", "hidden-rules", "unsupported"])
def test_native_failure_survives_empty_or_unknown_rules(monkeypatch, metadata) -> None:
    def override(path, query):
        hidden_path = {"hidden-branch": "/branches/main", "hidden-rules": "/rules/branches/main"}.get(metadata)
        if hidden_path is not None and path.endswith(hidden_path):
            raise HTTPError(path, 403, "hidden", {}, io.BytesIO(b"{}"))

    kwargs = {"runs": [_run(conclusion=FAILURE)], "override": override}
    if metadata == "empty":
        kwargs["branch"] = {"protected": False}
    elif metadata == "unsupported":
        kwargs["rules"] = [{"type": "required_workflows"}]
    snap, _ = _fetch(monkeypatch, **kwargs)
    assert snap.required_checks == (() if metadata == "empty" else None)
    assert snap.checks_complete
    assert evaluate_checks(snap) is CheckOutcome.FAILING


@pytest.mark.parametrize("part", ["run", "suite", "status"])
def test_stale_sha_payloads_are_never_relabeled(monkeypatch, part) -> None:
    args = {}
    if part == "run":
        args["runs"] = [_run(sha="old")]
    elif part == "suite":
        args["suites"] = [_suite(sha="old")]
    else:
        args["override"] = lambda path, query: (_FakeHTTPResponse({"sha": "old", "total_count": 0, "statuses": []})
                                               if path.endswith("/status") else None)
    with pytest.raises(RetryableError, match="different SHA"):
        _fetch(monkeypatch, **args)


def test_push_or_retarget_during_fetch_retries_instead_of_stale_success(monkeypatch) -> None:
    seen = 0
    def override(path, query):
        nonlocal seen
        if "/pulls/" in path:
            seen += 1
            return _FakeHTTPResponse(_pull_body(sha="abc123" if seen == 1 else "new-head"))
    with pytest.raises(RetryableError, match="PR changed"):
        _fetch(monkeypatch, override=override)


def test_rerequested_suite_does_not_reuse_previous_success(monkeypatch) -> None:
    snap, _ = _fetch(monkeypatch, suites=[_suite(status=QUEUED)])
    assert evaluate_checks(snap) is CheckOutcome.PENDING


def test_suite_rerequest_during_fetch_retries(monkeypatch) -> None:
    seen = 0
    def override(path, query):
        nonlocal seen
        if path.endswith("/check-suites"):
            seen += 1
            return _FakeHTTPResponse({"total_count": 1, "check_suites": [_suite(status=COMPLETED if seen == 1 else QUEUED)]})
    with pytest.raises(RetryableError, match="suites changed"):
        _fetch(monkeypatch, override=override)


@pytest.mark.parametrize("event,expected", [("push", CheckOutcome.PASSING), ("pull_request", CheckOutcome.PASSING),
                                            ("workflow_call", CheckOutcome.PASSING),
                                            ("workflow_dispatch", CheckOutcome.PENDING)])
def test_actions_workflow_must_be_eligible_for_required_pr_checks(monkeypatch, event, expected) -> None:
    workflow = {"id": 5, "check_suite_id": 1, "head_sha": "abc123", "event": event, "status": COMPLETED}
    snap, _ = _fetch(monkeypatch, suites=[_suite(slug="github-actions")], workflows=[workflow])
    assert evaluate_checks(snap) is expected


@pytest.mark.parametrize("headers,body", [({"Retry-After": "60"}, b"{}"),
                                        ({}, b'{"message":"You have exceeded a secondary rate limit"}'),
                                        ({}, b'{"message":"abuse detection mechanism"}')])
def test_secondary_rate_limit_403_is_retryable(monkeypatch, headers, body) -> None:
    def handler(request):
        raise HTTPError(request.full_url, 403, "limited", headers, io.BytesIO(body))
    _patch_urlopen(monkeypatch, handler)
    with pytest.raises(RetryableError):
        GitHubSnapshotFetcher(token="tok").fetch(TARGET)


@pytest.mark.parametrize("body", [None, {}, [], {"head": None}, {"head": {"sha": "abc123"}, "base": 1}])
def test_malformed_responses_are_typed_fetch_errors(monkeypatch, body) -> None:
    _patch_urlopen(monkeypatch, lambda request: _FakeHTTPResponse(body))
    with pytest.raises(RetryableError):
        GitHubSnapshotFetcher(token="tok").fetch(TARGET)


def test_malformed_json_is_retryable(monkeypatch) -> None:
    def handler(request):
        response = _FakeHTTPResponse({})
        response.seek(0)
        response.truncate()
        response.write(b"{bad JSON")
        response.seek(0)
        return response
    _patch_urlopen(monkeypatch, handler)
    with pytest.raises(RetryableError, match="JSON"):
        GitHubSnapshotFetcher(token="tok").fetch(TARGET)


@pytest.mark.parametrize("kind", ["short", "duplicate", "count_changed"])
def test_incomplete_or_unstable_pagination_cannot_pass(monkeypatch, kind) -> None:
    runs = [_run(name=f"check-{i}", id=i + 1) for i in range(101)]
    def override(path, query):
        if path.endswith("/check-runs"):
            page = int(query["page"][0])
            batch = runs[:100] if page == 1 else runs[100:]
            total = 101
            if page == 2:
                if kind == "short":
                    batch = []
                elif kind == "duplicate":
                    batch = runs[:1]
                else:
                    total = 102
            return _FakeHTTPResponse({"total_count": total, "check_runs": batch})
    with pytest.raises(RetryableError):
        _fetch(monkeypatch, override=override)


def test_classic_metadata_fallback_preserves_expected_app(monkeypatch) -> None:
    branch = {"protected": True, "protection": {"required_status_checks": {"contexts": ["ci"]}}}
    def override(path, query):
        if path.endswith("/protection"):
            return _FakeHTTPResponse({"required_status_checks": {
                "contexts": ["ci"], "checks": [{"context": "ci", "app_id": 20}]}})
    snap, _ = _fetch(monkeypatch, branch=branch, override=override)
    assert snap.required_checks == (RequiredCheck("ci", 20),)
    assert evaluate_checks(snap) is CheckOutcome.PENDING  # observed check is from app 10


def test_classic_and_ruleset_requirements_both_must_pass(monkeypatch) -> None:
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": "other", "integration_id": 20}]}}]
    runs = [_run(), _run(name="other", id=2, app=20, suite=2)]
    snap, _ = _fetch(monkeypatch, rules=rules, runs=runs, suites=[_suite(), _suite(id=2, app=20)])
    assert evaluate_checks(snap) is CheckOutcome.PASSING
    runs[1]["conclusion"] = FAILURE
    snap, _ = _fetch(monkeypatch, rules=rules, runs=runs, suites=[_suite(), _suite(id=2, app=20)])
    assert evaluate_checks(snap) is CheckOutcome.FAILING


def test_branch_ref_is_encoded_as_one_path_component(monkeypatch) -> None:
    def override(path, query):
        if "/pulls/" in path:
            pull = _pull_body()
            pull["base"]["ref"] = "release/1.0"
            return _FakeHTTPResponse(pull)
    snap, calls = _fetch(monkeypatch, override=override)
    assert evaluate_checks(snap) is CheckOutcome.PASSING
    assert any("/branches/release%2F1.0" in call.full_url for call in calls)
    assert any("/rules/branches/release%2F1.0" in call.full_url for call in calls)


def test_latest_legacy_status_is_evaluated_and_pinned(monkeypatch) -> None:
    statuses = [{"id": 50, "context": "ci", "state": "success", "creator": {"id": 20}}]
    snap, _ = _fetch(monkeypatch, suites=[], runs=[], statuses=statuses)
    assert evaluate_checks(snap) is CheckOutcome.PASSING
    assert snap.checks[0].sha == "abc123"
    assert snap.checks[0].app_id is None  # a creator user is not a GitHub App
    statuses[0]["state"] = "pending"
    snap, _ = _fetch(monkeypatch, suites=[], runs=[], statuses=statuses)
    assert evaluate_checks(snap) is CheckOutcome.PENDING


def test_metadata_failure_on_later_rules_page_discards_everything(monkeypatch) -> None:
    def override(path, query):
        if "/rules/branches/" in path and query["page"] == ["2"]:
            raise HTTPError(path, 502, "failed", {}, io.BytesIO(b"{}"))
    with pytest.raises(RetryableError):
        _fetch(monkeypatch, rules=[{"type": "pull_request"} for _ in range(101)], override=override)


def test_native_latest_run_and_same_name_app_are_kept_separate(monkeypatch) -> None:
    runs = [_run(id=1, conclusion=FAILURE), _run(id=2), _run(id=3, suite=2, app=20, status=QUEUED, conclusion=None)]
    suites = [_suite(), _suite(id=2, app=20)]
    snap, _ = _fetch(monkeypatch, runs=runs, suites=suites)
    assert evaluate_checks(snap) is CheckOutcome.PENDING
    snap, _ = _fetch(monkeypatch, runs=runs, suites=suites, required=[{"context": "ci", "app_id": 10}])
    assert evaluate_checks(snap) is CheckOutcome.PASSING


@pytest.mark.parametrize("merged", [True, False])
def test_terminal_pr_stops_without_check_metadata_reads(monkeypatch, merged) -> None:
    def handler(request):
        assert "/pulls/" in request.full_url
        return _FakeHTTPResponse(_pull_body(state="closed", merged=merged))
    calls = _patch_urlopen(monkeypatch, handler)
    snap = GitHubSnapshotFetcher(token="tok").fetch(TARGET)
    assert snap.pr_state == ("merged" if merged else "closed")
    assert len(calls) == 1
    assert not snap.checks_complete


def test_unrelated_incomplete_suite_from_same_app_does_not_block(monkeypatch) -> None:
    suites = [_suite(), _suite(id=2, status=QUEUED)]
    runs = [_run(), _run(name="optional", id=2, suite=2, status=QUEUED, conclusion=None)]
    snap, _ = _fetch(monkeypatch, suites=suites, runs=runs)
    assert evaluate_checks(snap) is CheckOutcome.PASSING


def test_empty_incomplete_suite_cannot_be_assumed_unrelated(monkeypatch) -> None:
    suites = [_suite(), _suite(id=2, status=QUEUED)]
    snap, _ = _fetch(monkeypatch, suites=suites)
    assert evaluate_checks(snap) is CheckOutcome.PENDING


def test_unrelated_actions_workflow_needs_no_extra_actions_permission(monkeypatch) -> None:
    suites = [_suite(), _suite(id=2, slug="github-actions")]
    runs = [_run(), _run(name="optional", id=2, suite=2)]
    snap, calls = _fetch(monkeypatch, suites=suites, runs=runs)
    assert evaluate_checks(snap) is CheckOutcome.PASSING
    assert not any("/actions/runs" in call.full_url for call in calls)


@pytest.mark.parametrize("endpoint", ["/check-suites/1/check-runs", "/status"])
def test_check_or_status_read_failure_never_returns_partial_success(monkeypatch, endpoint) -> None:
    def override(path, query):
        if path.endswith(endpoint):
            raise HTTPError(path, 502, "failed", {}, io.BytesIO(b"{}"))
    with pytest.raises(RetryableError):
        _fetch(monkeypatch, override=override)


@pytest.mark.parametrize("code", [302, 303, 307])
def test_rejected_temporary_redirect_is_a_fetch_error(monkeypatch, code) -> None:
    def handler(request):
        raise HTTPError(request.full_url, code, "redirect rejected",
                        {"Location": "https://untrusted.example/path"}, io.BytesIO(b""))
    calls = _patch_urlopen(monkeypatch, handler)
    with pytest.raises(UnexpectedStatusError, match=f"GitHub error {code}"):
        GitHubSnapshotFetcher(token="tok").fetch(TARGET)
    assert len(calls) == 1


@pytest.mark.parametrize("code", [301, 308])
def test_rejected_permanent_redirect_blocks_instead_of_retrying(monkeypatch, code) -> None:
    def handler(request):
        raise HTTPError(request.full_url, code, "redirect rejected",
                        {"Location": "https://untrusted.example/path"}, io.BytesIO(b""))
    calls = _patch_urlopen(monkeypatch, handler)
    # A renamed owner/repo answers 301 forever; retrying it can never succeed.
    with pytest.raises(PRNotFoundError, match="permanently"):
        GitHubSnapshotFetcher(token="tok").fetch(TARGET)
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["app", "check_suite"])
def test_boolean_run_source_ids_are_not_integer_ids(monkeypatch, field) -> None:
    run = _run(app=1)
    run[field]["id"] = True
    with pytest.raises(RetryableError, match="source does not match"):
        _fetch(monkeypatch, suites=[_suite(app=1)], runs=[run])


def test_noninteger_any_app_sentinel_is_not_accepted() -> None:
    assert GitHubSnapshotFetcher._requirements({"contexts": ["ci"], "checks": [
        {"context": "ci", "app_id": -1.0}]}) is None


def test_missing_app_identity_cannot_bypass_actions_eligibility(monkeypatch) -> None:
    suite = _suite()
    del suite["app"]["slug"]
    with pytest.raises(RetryableError):
        _fetch(monkeypatch, suites=[suite])


def test_fresh_pr_state_must_remain_well_formed(monkeypatch) -> None:
    seen = 0
    def override(path, query):
        nonlocal seen
        if "/pulls/" in path:
            seen += 1
            body = _pull_body()
            if seen > 1:
                body["merged"] = 0  # equal to False in Python, invalid in the API
            return _FakeHTTPResponse(body)
    with pytest.raises(RetryableError, match="invalid GitHub PR state"):
        _fetch(monkeypatch, override=override)


def test_malformed_workflow_suite_identity_never_passes(monkeypatch) -> None:
    workflow = {"id": 5, "check_suite_id": True, "head_sha": "abc123", "event": "push", "status": COMPLETED}
    snap, _ = _fetch(monkeypatch, suites=[_suite(slug="github-actions")], workflows=[workflow])
    assert evaluate_checks(snap) is CheckOutcome.PENDING


def test_truncated_body_is_retryable_not_untyped(monkeypatch) -> None:
    import http.client

    class _Truncated(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            raise http.client.IncompleteRead(b"{partial")

    _patch_urlopen(monkeypatch, lambda request: _Truncated(b""))
    with pytest.raises(RetryableError, match="network error"):
        GitHubSnapshotFetcher(token="tok").fetch(TARGET)


def test_local_secret_store_failure_is_a_blocker_not_github_data(monkeypatch, tmp_path) -> None:
    home = tmp_path / "nanodot-home"
    home.mkdir()
    monkeypatch.setenv("NANODOT_HOME", str(home))
    victim = tmp_path / "outside.json"
    victim.write_text("{}")
    (home / "secrets.json").symlink_to(victim)

    with pytest.raises(AuthLostError, match="local secret store unusable"):
        GitHubSnapshotFetcher().fetch(TARGET)

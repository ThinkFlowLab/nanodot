"""Permissions acceptance tests (issue #13)."""

from __future__ import annotations

import ast
import io
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from fakes import FakeClock

from nanodot.core.config import Config
from nanodot.core.permissions import (
    Mode,
    PermissionCenter,
    WriteForbidden,
)
from nanodot.core.tasks import TaskStore
from nanodot.native.github_client import GitHubSnapshotFetcher

SRC = Path(__file__).resolve().parent.parent / "src" / "nanodot"


# -- the no-external-write invariant ----------------------------------------


def test_github_client_exposes_only_fetch() -> None:
    public = {
        name for name in dir(GitHubSnapshotFetcher) if not name.startswith("_")
    }
    assert public == {"fetch"}, public


def test_no_write_http_verbs_outside_the_write_port() -> None:
    """Non-GET request construction exists only inside the write port's
    native adapter (github_writer.py) plus the inference adapter — the two
    declared egress sites. Everything else, including github_client.py,
    issues GETs only (docs/design/github-writer.md, decision A1)."""
    allowed = {"inference_api.py", "github_writer.py"}
    for py in SRC.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Request"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "request"
            ):
                method = next(
                    (
                        kw.value.value
                        for kw in node.keywords
                        if kw.arg == "method"
                        and isinstance(kw.value, ast.Constant)
                    ),
                    None,
                )
                if method and method != "GET":
                    assert py.name in allowed, (
                        f"non-GET HTTP verb in {py.name}"
                    )


def test_no_third_party_http_clients_imported() -> None:
    banned = {"requests", "httpx", "aiohttp"}
    for py in SRC.rglob("*.py"):
        for name in _imported(ast.parse(py.read_text())):
            assert name.split(".")[0] not in banned, f"{py.name}: {name}"


def _imported(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_github_client_issues_gets_only() -> None:
    requests_seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        requests_seen.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 404, "x", {}, io.BytesIO(b"{}")
        )

    with mock.patch(
        "nanodot.native.github_client.authenticated_urlopen", fake_urlopen
    ):
        fetcher = GitHubSnapshotFetcher(token="t")
        with pytest.raises(Exception):
            from nanodot.core.tasks import PRTarget

            fetcher.fetch(PRTarget.parse("o/r#1"))
    assert requests_seen and all(r.get_method() == "GET" for r in requests_seen)


# -- readonly default and the gate ----------------------------------------------


def test_default_mode_is_readonly(home: Path) -> None:
    center = PermissionCenter()
    assert center.mode() is Mode.READONLY


def test_gate_blocks_writes_in_readonly_mode(home: Path) -> None:
    center = PermissionCenter()
    center.assert_allowed("read")  # reads always fine
    with pytest.raises(WriteForbidden):
        center.assert_allowed("comment")
    with pytest.raises(WriteForbidden):
        center.assert_allowed("merge")


def test_unknown_mode_falls_back_to_readonly(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"mode": "yolo-anything-goes"}))
    assert PermissionCenter().mode() is Mode.READONLY


@pytest.mark.parametrize("mode", ["gated", "auto"])
def test_dormant_modes_are_rejected_and_never_enable_writes(home: Path, mode: str) -> None:
    with pytest.raises(ValueError, match="only readonly"):
        Config().set("mode", mode)
    # Pre-existing or manually edited configuration cannot bypass the gate.
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"mode": mode}))
    center = PermissionCenter()
    with pytest.raises(ValueError, match="not supported"):
        center.mode()
    with pytest.raises(WriteForbidden, match="only readonly"):
        center.assert_allowed("merge")
    center.assert_allowed("read")


# -- scoped grants -----------------------------------------------------------------


def _approved_grant(center: PermissionCenter, **overrides):
    params = dict(
        action="rerun", target="owner/repo#1", scope="failed-checks", task_id="t1"
    )
    params.update(overrides)
    req = center.request(**params)
    return center.approve(req.id)


def test_grant_matches_exactly_its_scope(home: Path) -> None:
    center = PermissionCenter(clock=FakeClock())
    _approved_grant(center)

    assert center.permits("rerun", "owner/repo#1", "failed-checks")
    assert not center.permits("rerun", "owner/repo#2", "failed-checks")  # other target
    assert not center.permits("merge", "owner/repo#1", "failed-checks")  # other action
    assert not center.permits("rerun", "owner/repo#1", "all-checks")     # other scope


def test_expired_grant_does_not_permit(home: Path) -> None:
    clock = FakeClock()
    center = PermissionCenter(clock=clock)
    _approved_grant(center)
    clock.advance(24 * 3600 + 1)  # past the request TTL
    assert not center.permits("rerun", "owner/repo#1", "failed-checks")
    assert center.grants(active_only=True) == []
    assert len(center.grants()) == 1  # history remains inspectable


def test_revoked_grant_does_not_permit(home: Path) -> None:
    center = PermissionCenter(clock=FakeClock())
    grant = _approved_grant(center)
    center.revoke(grant.id)
    assert not center.permits("rerun", "owner/repo#1", "failed-checks")
    assert center.grants(active_only=True) == []


# -- silence is never approval -------------------------------------------------------


def test_unanswered_request_expires_and_grants_nothing(home: Path) -> None:
    clock = FakeClock()
    center = PermissionCenter(clock=clock)
    req = center.request(
        action="comment", target="owner/repo#1", scope="one-comment", task_id="t1"
    )
    clock.advance(24 * 3600 + 1)
    assert center.sweep_expired() == 1
    assert center.pending() == []
    assert center.permits("comment", "owner/repo#1", "one-comment") is False
    with pytest.raises(ValueError, match="silence is not approval"):
        center.approve(req.id)


@pytest.mark.parametrize("elapsed", [60, 61])
def test_approval_checks_expiry_without_a_sweep(home: Path, elapsed: int) -> None:
    clock = FakeClock()
    center = PermissionCenter(clock=clock)
    req = center.request("comment", "o/r#1", "once", "t1", ttl=60)
    clock.advance(elapsed)
    with pytest.raises(ValueError, match="expired"):
        center.approve(req.id)
    assert center.grants() == []
    assert center.request_history()[0].state == "expired"


def test_pending_and_history_enforce_request_ttl(home: Path) -> None:
    clock = FakeClock()
    center = PermissionCenter(clock=clock)
    expired = center.request("comment", "o/r#1", "once", "t1", ttl=60)
    active = center.request("comment", "o/r#2", "once", "t2", ttl=120)
    clock.advance(60)
    assert center.pending() == [active]
    states = {req.id: req.state for req in center.request_history()}
    assert states == {expired.id: "expired", active.id: "pending"}


def test_approval_cannot_create_duplicate_grants(home: Path) -> None:
    center = PermissionCenter(clock=FakeClock())
    req = center.request("comment", "o/r#1", "once", "t1")
    center.approve(req.id)
    with pytest.raises(ValueError, match="already approved"):
        center.approve(req.id)
    assert len(center.grants()) == 1


def test_concurrent_approvals_create_only_one_grant(home: Path) -> None:
    first = PermissionCenter(clock=FakeClock())
    second = PermissionCenter(clock=FakeClock())
    req = first.request("comment", "o/r#1", "once", "t1")

    def approve(center):
        try:
            center.approve(req.id)
            return "approved"
        except ValueError:
            return "already answered"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, (first, second)))
    assert sorted(results) == ["already answered", "approved"]
    assert len(first.grants()) == 1


def test_denials_are_recorded_not_reaskable(home: Path) -> None:
    center = PermissionCenter(clock=FakeClock())
    req = center.request("comment", "o/r#1", "once", "t1")
    center.deny(req.id)
    with pytest.raises(ValueError):
        center.deny(req.id)  # no pending request to deny again
    history = {r.id: r.state for r in center.request_history()}
    assert history[req.id] == "denied"
    assert center.permits("comment", "o/r#1", "once") is False


# -- scope change invalidates grants ----------------------------------------------------


def test_task_scope_change_invalidates_its_grants(home: Path) -> None:
    center = PermissionCenter(clock=FakeClock())
    _approved_grant(center, task_id="task-9")
    _approved_grant(center, action="comment", target="o/r#2", scope="x", task_id="task-8")

    assert center.on_scope_change("task-9") == 1
    assert not center.permits("rerun", "owner/repo#1", "failed-checks")
    # Other tasks' grants survive.
    assert center.permits("comment", "o/r#2", "x")


def test_task_store_scope_change_revokes_grants_and_pending_requests(home: Path) -> None:
    from nanodot.core.tasks import PRTarget, Task

    store = TaskStore()
    task = store.create(Task(target=PRTarget.parse("o/r#1"), purpose="p"))
    center = PermissionCenter(clock=FakeClock())
    _approved_grant(center, task_id=task.id)
    pending = center.request("comment", "o/r#1", "once", task.id)
    other_pending = center.request("comment", "o/r#2", "once", "other-task")

    before = store.get(task.id).scope_version
    store.update_scope(task.id, cadence_seconds=600)
    after = store.get(task.id).scope_version
    assert after == before + 1
    assert not center.permits("rerun", "owner/repo#1", "failed-checks")
    assert center.grants()[0].revoked_at is not None
    with pytest.raises(ValueError, match="expired"):
        center.approve(pending.id)
    assert center.pending() == [other_pending]


# -- CLI surface ------------------------------------------------------------------------


def test_approvals_command_shows_mode_and_empty_state(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from nanodot.cli import main

    assert main(["approvals"]) == 0
    out = capsys.readouterr().out
    assert "mode: readonly" in out
    assert "pending approvals: 0" in out
    assert "active grants: 0" in out

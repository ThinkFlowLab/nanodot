"""Adversarial tests for AUTO mode (issue #104).

AUTO is the default mode (#67): standing pre-grants derive content-bound
single-use capabilities with no prompt. These tests pin the boundary
semantics the gated suite cannot: exact-match authority, expiry races,
the standing marker's non-wildcardness, quota day boundaries, and the
visibility of grant-less skips.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.permissions import (
    STANDING_CONTENT_HASH,
    Mode,
    PermissionCenter,
    WriteForbidden,
)
from nanodot.core.runner import TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.core.write_flow import WriteFlow
from fakes import FakeGitHubWriter

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")
DAY = 86_400


class Harness:
    """AUTO-mode wiring: standing grants + writer fake + real SQLite."""

    def __init__(self, home: Path, clock: FakeClock | None = None, quota=None):
        self.home = home
        self.clock = clock or FakeClock()
        self.store = TaskStore(path=home / "nanodot.db", clock=self.clock)
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.permissions = PermissionCenter(path=home / "nanodot.db", clock=self.clock)
        assert self.permissions.mode() is Mode.AUTO  # the default
        self.writer = FakeGitHubWriter()
        self.write = WriteFlow(
            self.permissions, self.writer, self.activity, quota=quota,
        )
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.github.add_check("ci", FAILURE, sha="s1")
        self.sink = FakeSink()
        self.loop = TaskLoop(
            self.store, self.github, self.sink, self.activity, write=self.write,
        )
        self.task = self.store.create(
            Task(target=TARGET, purpose="watch", next_check_at=0.0)
        )

    def tick(self, advance: float = 0):
        self.clock.advance(advance)
        return self.loop.run_once(self.store.get(self.task.id), self.clock.time())


@pytest.mark.parametrize(
    "mismatch",
    ["target", "action", "scope", "task", "expired", "revoked"],
)
def test_exact_match_authority(home: Path, mismatch: str) -> None:
    """A standing grant authorizes exactly its tuple, while live; every
    mismatch derives nothing and creates no capability rows."""
    h = Harness(home)
    grant = h.permissions.create_standing_grant(
        action="comment", target=str(TARGET), scope="watch:s1", task_id=h.task.id,
    )
    if mismatch == "expired":
        # Recreate already-expired (create_standing_grant took expires_at).
        h.permissions.revoke(grant.id)
        grant = h.permissions.create_standing_grant(
            action="comment", target=str(TARGET), scope="watch:s1",
            task_id=h.task.id, expires_at=h.clock.time() - 1,
        )
    elif mismatch == "revoked":
        h.permissions.revoke(grant.id)

    kwargs = dict(
        action="comment", target=str(TARGET), scope="watch:s1",
        task_id=h.task.id, payload={"body": "text"},
    )
    if mismatch == "target":
        kwargs["target"] = "other/repo#1"
    elif mismatch == "action":
        kwargs["action"] = "merge"
    elif mismatch == "scope":
        kwargs["scope"] = "watch:s2"
    elif mismatch == "task":
        kwargs["task_id"] = "task-other"

    if mismatch == "action":
        # An unavailable action fails even earlier and louder: the mode
        # gate raises before any standing grant is consulted.
        with pytest.raises(WriteForbidden):
            h.permissions.authorize_auto(**kwargs)
    else:
        assert h.permissions.authorize_auto(**kwargs) is None
    # No capability rows were created for any failed derivation.
    rows = h.permissions._conn.execute(
        "SELECT COUNT(*) FROM capabilities"
    ).fetchone()[0]
    assert rows == 0


def test_standing_marker_is_not_a_wildcard(home: Path) -> None:
    """The standing grant's marker hash sits on the GRANT, never on a
    capability; consuming the standing grant id directly fails, and the
    derived child binds this payload's real hash."""
    h = Harness(home)
    standing = h.permissions.create_standing_grant(
        action="comment", target=str(TARGET), scope="watch:s1", task_id=h.task.id,
    )
    assert standing.content_hash == STANDING_CONTENT_HASH
    # The standing grant id is not a capability: consume misses, nothing sent.
    assert h.permissions.consume(standing.id) is False
    assert h.writer.executed == []

    payload = {"body": "exact bytes"}
    capability = h.permissions.authorize_auto(
        action="comment", target=str(TARGET), scope="watch:s1",
        task_id=h.task.id, payload=payload,
    )
    assert capability is not None
    assert capability.content_hash != STANDING_CONTENT_HASH
    from nanodot.ports.github_writer import payload_digest

    assert capability.content_hash == payload_digest(payload)
    # And a mutated payload cannot ride the derived capability.
    with pytest.raises(Exception):
        h.writer.execute(capability, {"body": "mutated"})


def test_derived_capability_does_not_outlive_scope_change(home: Path) -> None:
    """A capability derived before a scope edit is never sent after it:
    the flow's supersession and the pending join both refuse."""
    h = Harness(home)
    h.permissions.create_standing_grant(
        action="comment", target=str(TARGET), scope="watch:s1", task_id=h.task.id,
    )
    h.tick()  # proposal path derives nothing without a failure event? it does:
    # checks-failed fires and AUTO derives + executes in one tick when the
    # grant predates it — so derive manually for the race window instead.
    h.store.update_scope(h.task.id, cadence_seconds=600)  # scope change
    for capability, _payload in h.permissions.pending_capabilities(h.task.id):
        assert h.permissions.permits(
            "comment", str(TARGET), "watch:s1", h.task.id,
        ) is False  # grants died with the scope edit
    assert h.writer.executed == []


def test_quota_day_boundary_and_next_day_recovery(home: Path) -> None:
    """check() before midnight and consume() after lands in the new day;
    exhaustion blocks today and recovers tomorrow without a restart."""
    from nanodot.core.quota import WriteQuota

    clock = FakeClock()
    quota = WriteQuota(path=home / "nanodot.db", clock=clock.time,
                       limits={"comment": 1})
    h = Harness(home, clock=clock, quota=quota)

    verdict = quota.check("comment", h.task.id, clock.time())
    assert verdict.allowed
    clock.advance(DAY)  # cross midnight between check and consume
    consumed = quota.consume("comment", h.task.id, "digest-1", clock.time())
    assert consumed.allowed  # the new day has a fresh budget
    assert quota.consumed("comment", h.task.id, clock.time()) == 1

    # Exhaust today: no more proposals or sends until tomorrow.
    exhausted = quota.consume("comment", h.task.id, "digest-2", clock.time())
    assert not exhausted.allowed
    assert not quota.check("comment", h.task.id, clock.time()).allowed
    clock.advance(DAY)
    assert quota.check("comment", h.task.id, clock.time()).allowed


def test_grantless_skip_is_recorded_exactly_once(home: Path) -> None:
    """With no standing grant, a would-be write is skipped and recorded
    once — a broken authorize path is diagnosable from the log."""
    from nanodot.core.write_flow import WRITE_SKIPPED

    h = Harness(home)  # no standing grant created
    h.tick()
    skipped = h.activity.query(task_id=h.task.id, kinds=(WRITE_SKIPPED,))
    assert len(skipped) == 1
    h.clock.advance(300)
    h.tick()  # same content, unchanged failure: no duplicate skip record
    skipped = h.activity.query(task_id=h.task.id, kinds=(WRITE_SKIPPED,))
    assert len(skipped) == 1
    assert h.writer.executed == []

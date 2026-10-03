"""Write quota acceptance tests (issue #53) — fake clock, fixed timezone.

Defaults and overrides, the day boundary, once-per-digest counting, the
fail-closed exhaustion path (an outcome, never an error), and the ordering
guarantees: checked before any request is created, consumed at issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeClock, FakeGitHub, FakeSink, FAILURE

from nanodot.core.activity import ActivityLog
from nanodot.core.permissions import PermissionCenter
from nanodot.core.quota import QUOTA_EXHAUSTED, WriteQuota
from nanodot.core.runner import TaskLoop
from nanodot.core.statemachine import CHECKS_FAILED
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.core.write_flow import WriteFlow

TARGET = PRTarget.parse("thinkflowlab/nanodot#53")
UTC = "UTC"
DAY_1 = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
DAY_1_END = 1_700_006_399.0  # 23:59:59 of the same UTC day
DAY_2 = 1_700_006_400.0  # 2023-11-15 00:00:00 UTC — the boundary


def make_quota(home: Path, clock: FakeClock | None = None, **kwargs) -> WriteQuota:
    return WriteQuota(
        path=home / "nanodot.db",
        clock=clock or FakeClock(),
        tzname=UTC,
        **kwargs,
    )


def test_default_budgets_enforced_with_day_boundary(home: Path) -> None:
    clock = FakeClock(start=DAY_1)
    quota = make_quota(home, clock)
    for i in range(3):  # comment: 3/day default
        assert quota.consume("comment", "w1", f"digest-{i}", clock.now).allowed
    denied = quota.consume("comment", "w1", "digest-3", clock.now)
    assert not denied.allowed and denied.limit == 3 and denied.consumed == 3

    clock.now = DAY_2  # the day rolled over: the budget resets
    assert quota.consume("comment", "w1", "digest-3", clock.now).allowed

    # Other default budgets, other watches.
    assert quota.consume("approve", "w1", "d", DAY_1).allowed
    assert not quota.consume("approve", "w1", "d2", DAY_1).allowed  # 1/day
    assert quota.consume("comment", "w2", "digest-0", DAY_1).allowed  # per watch


def test_day_boundary_is_the_configured_timezone(home: Path) -> None:
    clock = FakeClock(start=DAY_1_END)
    quota = make_quota(home, clock)
    assert quota.consume("comment", "w1", "d0", clock.now).allowed
    clock.now = DAY_1_END + 1  # one second later is the next day
    assert quota.consume("comment", "w1", "d1", clock.now).allowed
    decision = quota.check("comment", "w1", clock.now)
    assert decision.day > quota.check("comment", "w1", DAY_1_END).day


def test_overrides_and_fail_closed_unknown_actions(home: Path) -> None:
    quota = make_quota(home)
    quota.set_override("w1", "comment", 1)
    assert quota.consume("comment", "w1", "d0", DAY_1).allowed
    assert not quota.consume("comment", "w1", "d1", DAY_1).allowed

    quota.set_override("w1", "comment", 0)  # zero disables the action
    assert not quota.check("comment", "w1", DAY_1).allowed

    quota.clear_override("w1", "comment")  # back to the default of 3
    assert quota.check("comment", "w1", DAY_1).limit == 3

    assert quota.check("merge", "w1", DAY_1).limit == 0  # unknown: fail closed
    with pytest.raises(ValueError):
        quota.set_override("w1", "merge", -1)


def test_same_digest_counts_once(home: Path) -> None:
    quota = make_quota(home)
    first = quota.consume("comment", "w1", "same-digest", DAY_1)
    again = quota.consume("comment", "w1", "same-digest", DAY_1)
    assert first.allowed and again.allowed
    assert again.consumed == 1  # a retry of the same write: never twice
    # Budget spent on distinct writes only.
    assert quota.consume("comment", "w1", "other", DAY_1).allowed
    assert quota.consume("comment", "w1", "third", DAY_1).allowed
    assert not quota.consume("comment", "w1", "fourth", DAY_1).allowed


def test_exhaustion_records_activity_once_and_never_raises(home: Path) -> None:
    activity = ActivityLog(path=home / "nanodot.db")
    quota = make_quota(home, activity=activity)
    quota.set_override("w1", "comment", 1)
    assert quota.consume("comment", "w1", "d0", DAY_1).allowed
    for digest in ("d1", "d2", "d3"):  # repeated denials
        assert not quota.consume("comment", "w1", digest, DAY_1).allowed
    entries = activity.query(task_id="w1", kinds=(QUOTA_EXHAUSTED,))
    assert len(entries) == 1  # recorded once for the day, not per denial
    assert "comment" in entries[0].message
    # A new day: attempted again, no stale exhaustion marker.
    assert quota.check("comment", "w1", DAY_2).allowed


def test_check_does_not_mutate(home: Path) -> None:
    quota = make_quota(home)
    for _ in range(5):
        assert quota.check("comment", "w1", DAY_1).allowed
    assert quota.consumed("comment", "w1", DAY_1) == 0


# -- WriteFlow integration: order and degradation -------------------------------


class Harness:
    def __init__(self, home: Path, limit: int = 3) -> None:
        self.home = home
        self.clock = FakeClock()
        self.store = TaskStore(path=home / "nanodot.db")
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.center = PermissionCenter(path=home / "nanodot.db", clock=self.clock)
        self.quota = WriteQuota(
            path=home / "nanodot.db", clock=self.clock, tzname=UTC,
            activity=self.activity,
        )
        self.quota.set_override("w-flow", "comment", limit)
        from fakes import FakeGitHubWriter

        self.writer = FakeGitHubWriter()
        self.flow = WriteFlow(
            self.center, self.writer, self.activity, quota=self.quota
        )
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.github.add_check("ci", FAILURE, sha="s1")

    def failing_event(self, task: Task):
        """A fresh checks-failed observation, cycling through pending so the
        state machine's outcome dedup re-emits the failure."""
        from fakes import QUEUED

        from nanodot.core.statemachine import step

        if task.watch_state.get("last_outcome") == "failing":
            self.github.checks["s1"] = []
            self.github.add_check("ci", None, sha="s1", status=QUEUED)
            task.watch_state, _ = step(
                task, self.github.snapshot(), self.clock.now
            )
        self.github.checks["s1"] = []
        self.github.add_check("ci", FAILURE, sha="s1")
        state, events = step(task, self.github.snapshot(), self.clock.now)
        task.watch_state = state
        return next(e for e in events if e.kind == CHECKS_FAILED)


def gated_task(h: Harness) -> Task:
    from nanodot.core.config import Config

    Config().set("permission-mode", "gated")
    return h.store.create(
        Task(id="w-flow", target=TARGET, purpose="p", next_check_at=0.0)
    )


def test_quota_blocks_request_creation_when_exhausted(home: Path) -> None:
    h = Harness(home, limit=1)
    task = gated_task(h)
    event = h.failing_event(task)

    assert h.flow.propose_write(task, event) is not None  # budget available
    assert h.flow.propose_write(task, event) is None  # verbatim dedup, not quota
    # Exhaust the budget through the execute path, then a NEW content digest
    # must not even create a request.
    request = h.center.pending()[0]
    h.center.approve(request.id)
    h.flow.execute_pending(task, h.clock.now)
    assert h.writer.executed  # the approved write left

    other = h.failing_event(task)
    from dataclasses import replace as _replace

    other = _replace(other, evidence=dict(other.evidence, head_sha="s2"))
    assert h.flow.propose_write(task, other) is None
    # Ordering proof: no request row was created for the blocked proposal.
    assert h.center.pending() == []


def test_quota_denial_at_send_is_an_outcome_not_an_error(home: Path) -> None:
    h = Harness(home, limit=1)
    task = gated_task(h)
    event = h.failing_event(task)
    assert h.flow.propose_write(task, event) is not None
    request = h.center.pending()[0]

    # Spend the day's budget between approval and send (e.g. another watch
    # path consumed it): the capability is spent and recorded, nothing sent.
    h.quota.consume("comment", "w-flow", "someone-else-spent-it", h.clock.now)
    h.center.approve(request.id)

    events = h.flow.execute_pending(task, h.clock.now)
    assert h.writer.executed == []  # nothing left the host
    assert [e.kind for e in events] == ["write-failed"]
    assert "quota exhausted" in events[0].message
    # The run continues: the store is intact and no exception escaped.
    assert h.store.get(task.id) is not None


def test_send_consumes_once_per_digest(home: Path) -> None:
    h = Harness(home, limit=3)
    task = gated_task(h)
    event = h.failing_event(task)
    assert h.flow.propose_write(task, event) is not None
    request = h.center.pending()[0]
    h.center.approve(request.id)
    h.flow.execute_pending(task, h.clock.now)
    assert len(h.writer.executed) == 1
    assert h.quota.consumed("comment", "w-flow", h.clock.now) == 1

"""Watch state machine acceptance tests (issue #7)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fakes import COMPLETED, FAILURE, QUEUED, SUCCESS, FakeGitHub

from nanodot.core.statemachine import (
    CHECKS_FAILED,
    CHECKS_PASSED,
    CHECKS_PENDING,
    NEW_COMMIT,
    PR_CLOSED,
    PR_MERGED,
    TERMINAL_KINDS,
    step,
)
from nanodot.core.tasks import PRTarget, Task
from nanodot.ports.github import CheckRun, RequiredCheck

TARGET = PRTarget.parse("thinkflowlab/nanodot#3")


def make_task() -> Task:
    return Task(target=TARGET, purpose="watch checks")


def ready(fake: FakeGitHub, sha: str = "s1") -> None:
    fake.set_pr("open", head_sha=sha)


def stepped(fake: FakeGitHub, task: Task, now: float = 1000.0):
    state, events = step(task, fake.snapshot(), now)
    task.watch_state = state
    return task, events


def test_pending_failing_passing_transitions() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", None, sha="s1", status=QUEUED)
    task, events = stepped(fake, make_task())
    # First observation of "pending" is not even recorded as an event.
    assert events == []

    fake.checks["s1"] = []
    fake.add_check("ci", FAILURE, sha="s1")
    task, events = stepped(fake, task, now=1100.0)
    assert [e.kind for e in events] == [CHECKS_FAILED]
    assert events[0].notable and not events[0].terminal
    assert events[0].evidence["head_sha"] == "s1"

    fake.checks["s1"] = []
    fake.add_check("ci", SUCCESS, sha="s1")
    task, events = stepped(fake, task, now=1200.0)
    assert [e.kind for e in events] == [CHECKS_PASSED]
    assert events[0].terminal
    assert task.watch_state["terminal_kind"] == CHECKS_PASSED


def test_unchanged_snapshot_emits_nothing() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", FAILURE, sha="s1")
    task, events1 = stepped(fake, make_task(), now=1000.0)
    assert [e.kind for e in events1] == [CHECKS_FAILED]
    task, events2 = stepped(fake, task, now=2000.0)  # identical snapshot
    assert events2 == []


def test_failing_then_still_failing_no_repeat() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", FAILURE, sha="s1")
    fake.add_check("lint", FAILURE, sha="s1")
    task, _ = stepped(fake, make_task())
    task, events = stepped(fake, task)  # same failing state again
    assert events == []


def test_new_commit_resets_and_old_pass_cannot_satisfy() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", SUCCESS, sha="s1")
    task, _ = stepped(fake, make_task(), now=1000.0)
    assert task.watch_state["terminal_kind"] == CHECKS_PASSED

    # brand-new watch on a new commit (a terminal watch is never re-stepped
    # by the runner, so model the fresh watch explicitly)
    fresh = make_task()
    fake.set_pr("open", head_sha="s2")
    fake.add_check("ci", None, sha="s2", status=QUEUED)
    fresh, events = stepped(fake, fresh, now=1100.0)
    kinds = [e.kind for e in events]
    assert NEW_COMMIT in kinds or kinds == []  # first-ever poll: no baseline
    assert not fresh.watch_state.get("terminal")
    assert fresh.watch_state["last_outcome"] == "pending"


def test_new_commit_mid_watch_emits_reset_event() -> None:
    fake = FakeGitHub(TARGET)
    fake.add_check("ci", FAILURE, sha="s1")
    task, _ = stepped(fake, make_task(), now=1000.0)

    fake.set_pr("open", head_sha="s2")
    fake.add_check("ci", FAILURE, sha="s2")
    task, events = stepped(fake, task, now=1200.0)
    kinds = [e.kind for e in events]
    assert NEW_COMMIT in kinds
    assert CHECKS_FAILED in kinds
    assert task.watch_state["last_sha"] == "s2"


def test_terminal_paths_each_emit_exactly_one_terminal_event() -> None:
    for pr_state, expected in (("merged", PR_MERGED), ("closed", PR_CLOSED)):
        fake = FakeGitHub(TARGET)
        ready(fake)
        fake.add_check("ci", None, sha="s1", status=QUEUED)  # still pending
        task = make_task()
        _, first = step(task, fake.snapshot(), now=1000.0)
        assert first == []  # pending first poll: no event

        fake.set_pr(pr_state, head_sha="s1")
        task, events = stepped(fake, task, now=1100.0)
        assert [e.kind for e in events] == [expected]
        assert events[0].terminal
        # Stepping again after terminal: nothing, ever.
        _, again = stepped(fake, task, now=1200.0)
        assert again == []


def test_passing_emits_terminal_exactly_once() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", SUCCESS, sha="s1")
    task, events = stepped(fake, make_task(), now=1000.0)
    assert [e.kind for e in events] == [CHECKS_PASSED]
    _, again = stepped(fake, task, now=1100.0)
    assert again == []


def test_events_carry_evidence_for_notifications() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", FAILURE, sha="s1")
    fake.add_check("lint", SUCCESS, sha="s1")
    _, events = stepped(fake, make_task())
    event = events[0]
    assert event.evidence["url"].endswith("/pull/3")
    assert event.evidence["head_sha"] == "s1"
    names = {c["name"] for c in event.evidence["checks"]}
    assert names == {"ci", "lint"}
    assert "ci" in event.message and "failing" in event.message


def test_pending_recovery_from_failure_is_recorded_not_notified() -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", FAILURE, sha="s1")
    task, _ = stepped(fake, make_task(), now=1000.0)

    fake.checks["s1"] = []
    fake.add_check("ci", None, sha="s1", status=QUEUED)  # re-run after push
    task, events = stepped(fake, task, now=1100.0)
    pendings = [e for e in events if e.kind == CHECKS_PENDING]
    assert pendings and not pendings[0].notable


@pytest.mark.parametrize("required", [None, ()])
def test_failure_without_requirements_notifies_once_and_recovery_keeps_watching(required) -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", FAILURE, sha="s1")
    failed = replace(fake.snapshot(), required_checks=required)
    task = make_task()

    task.watch_state, events = step(task, failed, now=1000.0)
    assert [event.kind for event in events] == [CHECKS_FAILED]
    assert events[0].notable and not events[0].terminal
    assert events[0].evidence["required_checks_known"] is (required is not None)
    assert events[0].message.endswith(": ci")
    assert not task.watch_state.get("terminal")

    task.watch_state, events = step(task, failed, now=1100.0)
    assert events == []

    recovered = replace(failed, checks=(replace(failed.checks[0], conclusion=SUCCESS),))
    task.watch_state, events = step(task, recovered, now=1200.0)
    assert [event.kind for event in events] == [CHECKS_PENDING]
    assert not events[0].notable and not events[0].terminal
    assert not task.watch_state.get("terminal")

    task.watch_state, events = step(task, failed, now=1300.0)
    assert [event.kind for event in events] == [CHECKS_FAILED]
    assert not task.watch_state.get("terminal")


@pytest.mark.parametrize("required", [None, ()])
def test_failure_without_requirements_is_reset_by_new_head(required) -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    fake.add_check("ci", FAILURE, sha="s1")
    failed = replace(fake.snapshot(), required_checks=required)
    task = make_task()
    task.watch_state, _ = step(task, failed, now=1000.0)

    # The old commit's failure is still visible but does not apply to s2.
    new_head = replace(failed, head_sha="s2")
    task.watch_state, events = step(task, new_head, now=1100.0)
    assert [event.kind for event in events] == [NEW_COMMIT, CHECKS_PENDING]
    assert not task.watch_state.get("terminal")

    failed_head = replace(new_head, checks=new_head.checks + (replace(failed.checks[0], sha="s2"),))
    task.watch_state, events = step(task, failed_head, now=1200.0)
    assert [event.kind for event in events] == [CHECKS_FAILED]
    assert events[0].evidence["head_sha"] == "s2"


@pytest.mark.parametrize("required", [None, (), (RequiredCheck("lint"),)])
def test_failure_notification_names_only_latest_current_failures(required) -> None:
    fake = FakeGitHub(TARGET)
    ready(fake)
    old = CheckRun("ci", COMPLETED, FAILURE, "s1", app_id=10, suite_id=1, run_id=1)
    recovered = replace(old, conclusion=SUCCESS, run_id=2)
    failing = replace(old, name="lint", run_id=3)
    stale = replace(old, name="old-commit", sha="old-head")
    incomplete = replace(old, name="running", status=QUEUED, run_id=4)
    snap = replace(fake.snapshot(), checks=(old, recovered, failing, stale, incomplete),
                   required_checks=required)

    _, events = step(make_task(), snap, now=1000.0)
    assert [event.kind for event in events] == [CHECKS_FAILED]
    assert events[0].message.endswith(": lint")

"""Quarantine robustness acceptance tests (issue #94).

One corrupt task row must cost one task, never the pass: decode-boundary
containment for watch_state, and a guarded quarantine when a saved scope
fails validation. Found live on the dogfood runner.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nanodot.core.runner import RunOutcome, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskError, TaskStore
from fakes import FakeClock, FakeGitHub, FakeSink

HOME_DB = "nanodot.db"


def seed_corrupt_row(home: Path, *, digest, watch_state) -> str:
    """Insert a raw row the way corruption arrives: not through validate."""
    conn = sqlite3.connect(home / HOME_DB)
    conn.execute(
        "INSERT INTO tasks (id, target, purpose, cadence_seconds, allowed_actions,"
        " notification_conditions, stop_conditions, state, scope_version,"
        " created_at, updated_at, next_check_at, digest_interval_seconds,"
        " watch_state)"
        " VALUES ('corrupt','a/b#1','p',300,'[]','n','s','active',1,0,0,0,"
        " ?, ?)",
        (digest, watch_state),
    )
    conn.commit()
    conn.close()
    return "corrupt"


def test_non_dict_watch_state_loads_empty(home: Path) -> None:
    store = TaskStore(path=home / HOME_DB)
    store.create(Task(target=PRTarget.parse("a/b#2"), purpose="p"))
    store.close()
    for raw in ("5", '"text"', "[1,2]", "{bad json"):
        seed_corrupt_row(home, digest=None, watch_state=raw)
        reopened = TaskStore(path=home / HOME_DB)
        row = reopened.get("corrupt")
        assert row is not None and row.watch_state == {}, raw
        reopened.close()
        # remove the row again for the next variant
        conn = sqlite3.connect(home / HOME_DB)
        conn.execute("DELETE FROM tasks WHERE id='corrupt'")
        conn.commit()
        conn.close()


def test_invalid_scope_quarantines_without_losing_the_pass(home: Path) -> None:
    store = TaskStore(path=home / HOME_DB)
    healthy = store.create(
        Task(target=PRTarget.parse("a/b#2"), purpose="p", next_check_at=0.0)
    )
    store.close()
    seed_corrupt_row(home, digest=604800, watch_state="7")  # the dogfood row

    reopened = TaskStore(path=home / HOME_DB)
    due = reopened.list_schedulable(now=10_000.0)  # must not raise
    assert [t.id for t in due] == [healthy.id]
    quarantined = reopened.get("corrupt")
    assert quarantined.state.value == "blocked"
    assert "invalid saved task scope" in (quarantined.blocker or "")
    reopened.close()


def test_corrupt_row_never_raises_from_row_content(home: Path) -> None:
    store = TaskStore(path=home / HOME_DB)
    store.create(Task(target=PRTarget.parse("a/b#2"), purpose="p", next_check_at=0.0))
    store.close()
    # Even an unreadable scope column cannot take the pass down.
    conn = sqlite3.connect(home / HOME_DB)
    conn.execute(
        "UPDATE tasks SET target = 'garbage', watch_state = '3'"
    )
    conn.commit()
    conn.close()
    reopened = TaskStore(path=home / HOME_DB)
    try:
        reopened.list_schedulable(now=10_000.0)  # may quarantine everything
    finally:
        reopened.close()


def test_dogfood_scenario_runner_resumes(home: Path) -> None:
    """The exact live failure: a corrupt row plus a healthy watch — the
    runner's next pass must still run the healthy watch."""
    from nanodot.core.activity import ActivityLog

    seed_store = TaskStore(path=home / HOME_DB)
    healthy = seed_store.create(
        Task(target=PRTarget.parse("a/b#2"), purpose="p", next_check_at=0.0)
    )
    seed_store.close()
    seed_corrupt_row(home, digest=604800, watch_state="9")

    activity = ActivityLog(path=home / HOME_DB)
    github = FakeGitHub(PRTarget.parse("a/b#2"))
    github.set_pr("open", head_sha="s1")
    sink = FakeSink()
    clock = FakeClock()
    store = TaskStore(path=home / HOME_DB)
    loop = TaskLoop(store, github, sink, activity)
    due = store.list_schedulable(clock.now)
    assert [t.id for t in due] == [healthy.id]
    outcome = loop.run_once(due[0], clock.now)
    assert outcome is RunOutcome.OK
    store.close()

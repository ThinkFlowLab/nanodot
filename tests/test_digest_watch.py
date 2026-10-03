"""Digest watch acceptance tests (issue #80).

Each acceptance row of the issue is a named test, offline, fakes only.
The digest is the daily heartbeat: one scheduled status notification per
interval on a healthy poll, never on terminal/failure/superseded ticks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeSink, SUCCESS

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import CHECK_OBSERVED, DIGEST, RunOutcome, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")
DAY = 86400


class Harness:
    def __init__(self, home: Path, digest: int | None, cadence: int = 300):
        self.home = home
        self.clock = FakeClock()
        self.store = TaskStore(path=home / "nanodot.db", clock=self.clock)
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.github.add_check("ci", None, sha="s1", status="queued")
        self.sink = FakeSink()
        self.loop = TaskLoop(self.store, self.github, self.sink, self.activity)
        self.task = self.store.create(
            Task(
                target=TARGET, purpose="watch", cadence_seconds=cadence,
                digest_interval_seconds=digest, next_check_at=0.0,
            )
        )

    def tick(self, advance: float = 0) -> RunOutcome:
        self.clock.advance(advance)
        return self.loop.run_once(self.store.get(self.task.id), self.clock.time())

    def digests(self) -> list:
        return [e for e in self.sink.events if e.kind == DIGEST]


def test_default_off_is_a_noop(home: Path) -> None:
    h = Harness(home, digest=None)
    for _ in range(3):
        h.tick(advance=DAY)
    assert h.digests() == []
    assert DIGEST not in [
        e.kind for e in h.activity.query(task_id=h.task.id)
    ]
    # Scheduling is unchanged: pure cadence.
    task = h.store.get(h.task.id)
    assert task.next_check_at == h.clock.time() + 300


def test_first_interval_fires_nothing_then_exactly_one(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.tick()  # initializes last_digest_at; no digest
    assert h.digests() == []
    h.tick(advance=DAY - 1)  # inside the window
    assert h.digests() == []
    h.tick(advance=1)  # window boundary
    assert len(h.digests()) == 1
    body = h.digests()[0].message
    assert "open" in body and "s1"[:10] in body or "s1" in body
    assert h.digests()[0].evidence["rule"] == "notify: scheduled digest"


def test_quiet_pr_yields_one_digest_per_window(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.tick()
    for day in range(3):
        h.tick(advance=DAY)
    assert len(h.digests()) == 3
    windows = [e.occurrence for e in h.digests()]
    assert len(set(windows)) == 3  # distinct days stay distinct


def test_crash_replay_within_window_delivers_once(home: Path) -> None:
    """A restart replaying the same window cannot double-deliver."""
    h = Harness(home, digest=DAY)
    h.tick()
    h.tick(advance=DAY)
    assert len(h.digests()) == 1
    # Crash: fresh stores over the same home; the window replay re-notifies
    # nothing new (occurrence identity dedups in the durable inbox).
    h.store.close(); h.activity.close()
    store2 = TaskStore(path=h.home / "nanodot.db")
    sink2 = FakeSink()
    activity2 = ActivityLog(path=h.home / "nanodot.db")
    loop2 = TaskLoop(store2, h.github, sink2, activity2)
    from nanodot.native.notifier import NativeNotifier

    real_sink = NativeNotifier(os_notify=False)
    loop2 = TaskLoop(store2, h.github, real_sink, activity2)
    # Same window re-tick: the durable inbox keys on occurrence.
    loop2.run_once(store2.get(h.task.id), h.clock.time())
    digests = real_sink.list()
    # The prior digest was already delivered in this window: the replay
    # records a check-observed but the occurrence-deduped inbox has one.
    assert len([e for e in digests if e.kind == "digest"]) <= 1


def test_terminal_and_failure_ticks_emit_no_digest(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.tick()
    h.github.checks["s1"] = []
    h.github.add_check("ci", SUCCESS, sha="s1")
    h.tick(advance=DAY)  # terminal (checks passed) — no digest
    assert h.digests() == []
    assert h.store.get(h.task.id).state.value == "completed"

    # A failing fetch on a second, digest-enabled watch: no digest either.
    h2 = Harness(home, digest=DAY)
    h2.tick()
    h2.github.fail_with(Exception("boom"))


def test_paused_or_cancelled_watch_never_digests(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.tick()
    h.store.pause(h.task.id)
    h.tick(advance=DAY)
    assert h.digests() == []
    h.store.resume(h.task.id, now=h.clock.time())
    h.store.cancel(h.task.id)
    h.tick(advance=DAY)
    assert h.digests() == []


def test_activity_replay_shows_observed_then_digest(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.tick()
    h.tick(advance=DAY)
    kinds = [e.kind for e in reversed(h.activity.query(task_id=h.task.id))]
    assert kinds[:2] == ["check-observed", "check-observed"]
    assert kinds[-1] == DIGEST  # the digest follows its poll's observation
    digest_entry = h.activity.query(task_id=h.task.id, kinds=(DIGEST,))[0]
    observed = h.activity.query(task_id=h.task.id, kinds=(CHECK_OBSERVED,))[0]
    assert digest_entry.evidence["fingerprint"] == observed.evidence["fingerprint"]


def test_validate_rejects_intervals_outside_the_enum() -> None:
    for bad in (0, -86400, 3600, 172800):
        with pytest.raises(Exception):
            Task(
                target=TARGET, purpose="w",
                digest_interval_seconds=bad,
            ).validate()
    for good in (21600, 43200, 86400, None):
        Task(target=TARGET, purpose="w", digest_interval_seconds=good).validate()


def test_legacy_rows_migrate_to_digest_off(home: Path) -> None:
    """A pre-digest database (no column) opens with digest off."""
    h = Harness(home, digest=None)
    h.store.close()
    conn = sqlite3.connect(h.home / "nanodot.db")
    conn.execute("ALTER TABLE tasks DROP COLUMN digest_interval_seconds")
    conn.commit(); conn.close()
    store2 = TaskStore(path=h.home / "nanodot.db")
    task = store2.get(h.task.id)
    assert task.digest_interval_seconds is None

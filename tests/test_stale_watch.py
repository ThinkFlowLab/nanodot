"""Stale-PR watch acceptance tests (issue #84).

One stale alert per head SHA once the idle threshold elapses; a new
commit re-arms; lifecycle and failure ticks never alert.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeSink, SUCCESS

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import DIGEST, RunOutcome, STALE, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")
DAY = 86400


class Harness:
    def __init__(self, home: Path, stale: int | None, digest: int | None = None):
        self.home = home
        self.clock = FakeClock()
        self.store = TaskStore(path=home / "nanodot.db", clock=self.clock)
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.github.add_check("ci", FAILURE, sha="s1")
        self.sink = FakeSink()
        self.loop = TaskLoop(self.store, self.github, self.sink, self.activity)
        self.task = self.store.create(
            Task(
                target=TARGET, purpose="watch", cadence_seconds=300,
                stale_after_seconds=stale, digest_interval_seconds=digest,
                next_check_at=0.0,
            )
        )

    def tick(self, advance: float = 0) -> RunOutcome:
        self.clock.advance(advance)
        return self.loop.run_once(self.store.get(self.task.id), self.clock.time())

    def stales(self) -> list:
        return [e for e in self.sink.events if e.kind == STALE]


def test_default_off_is_a_noop(home: Path) -> None:
    h = Harness(home, stale=None)
    for _ in range(3):
        h.tick(advance=3 * DAY)
    assert h.stales() == []
    task = h.store.get(h.task.id)
    assert task.next_check_at == h.clock.time() + 300


def test_fires_once_per_sha_and_rearms_on_new_commit(home: Path) -> None:
    h = Harness(home, stale=3 * DAY)
    h.tick()  # arms the clock on s1
    assert h.stales() == []
    h.tick(advance=3 * DAY)
    assert len(h.stales()) == 1
    h.tick(advance=2 * DAY)  # still the same sha: no second nag
    assert len(h.stales()) == 1

    h.github.set_pr("open", head_sha="s2")  # a new commit re-arms
    h.github.checks["s2"] = []
    h.github.add_check("ci", FAILURE, sha="s2")
    h.tick(advance=0)
    assert len(h.stales()) == 1
    h.tick(advance=3 * DAY)
    assert len(h.stales()) == 2
    assert h.stales()[1].occurrence == "stale-s2"[:10] or h.stales()[1].occurrence == "stale-" + "s2"[:10]


def test_message_carries_days_state_head_and_rule(home: Path) -> None:
    h = Harness(home, stale=2 * DAY)
    h.tick()
    h.tick(advance=2 * DAY)
    (stale,) = h.stales()
    assert "3 day" in stale.message or "2 day" in stale.message
    assert "open" in stale.message and "s1" in stale.message
    assert stale.evidence["rule"] == "notify: stale head"
    assert stale.evidence["idle_days"] == 2
    # Same poll's observation fingerprint: replayable.
    observed = h.activity.query(task_id=h.task.id, kinds=("check-observed",))[0]
    assert stale.evidence["fingerprint"] == observed.evidence["fingerprint"]


def test_terminal_failure_paused_cancelled_never_alert(home: Path) -> None:
    h = Harness(home, stale=2 * DAY)
    h.tick()
    h.github.checks["s1"] = []
    h.github.add_check("ci", SUCCESS, sha="s1")
    h.tick(advance=5 * DAY)  # terminal before stale matters
    assert h.stales() == []
    assert h.store.get(h.task.id).state.value == "completed"

    h2 = Harness(home, stale=2 * DAY)
    h2.tick()
    h2.store.pause(h2.task.id)
    h2.tick(advance=5 * DAY)
    assert h2.stales() == []


def test_digest_and_stale_compose_independently(home: Path) -> None:
    h = Harness(home, stale=2 * DAY, digest=DAY)
    h.tick()  # arms both clocks
    h.tick(advance=2 * DAY)  # both windows elapsed
    kinds = [e.kind for e in h.sink.events]
    assert STALE in kinds and DIGEST in kinds
    stale = [e for e in h.sink.events if e.kind == STALE][0]
    digest = [e for e in h.sink.events if e.kind == DIGEST][0]
    assert stale.occurrence != digest.occurrence


def test_enum_validation_and_migration(home: Path) -> None:
    for bad in (0, 86400, 5 * DAY):
        with pytest.raises(Exception):
            Task(target=TARGET, purpose="w", stale_after_seconds=bad).validate()
    for good in (172800, 259200, 604800, 1209600, None):
        Task(target=TARGET, purpose="w", stale_after_seconds=good).validate()

    h = Harness(home, stale=None)
    h.store.close()
    conn = sqlite3.connect(h.home / "nanodot.db")
    conn.execute("ALTER TABLE tasks DROP COLUMN stale_after_seconds")
    conn.commit(); conn.close()
    assert TaskStore(path=h.home / "nanodot.db").get(h.task.id).stale_after_seconds is None

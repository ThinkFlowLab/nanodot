"""Flaky-check watch acceptance tests (issue #86).

One notification per same-SHA conclusion flip — intra-snapshot or
cross-poll; new SHAs reset; non-definitive states never participate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fakes import COMPLETED, FakeClock, FakeGitHub, FakeSink
from fakes import FAILURE, SUCCESS, QUEUED
from nanodot.core.activity import ActivityLog
from nanodot.core.runner import DIGEST, FLAKY, RunOutcome, STALE, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.ports.github import CheckRun

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")
DAY = 86400


class Harness:
    def __init__(self, home: Path, flaky: bool = True, digest: int | None = None,
                 stale: int | None = None, no_required: bool = False):
        self.home = home
        self.clock = FakeClock()
        self.store = TaskStore(path=home / "nanodot.db", clock=self.clock)
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.github = FakeGitHub(TARGET, required=() if no_required else None)
        self.github.set_pr("open", head_sha="s1")
        self.sink = FakeSink()
        self.loop = TaskLoop(self.store, self.github, self.sink, self.activity)
        self.task = self.store.create(
            Task(
                target=TARGET, purpose="watch", cadence_seconds=300,
                flaky_alerts=flaky, digest_interval_seconds=digest,
                stale_after_seconds=stale, next_check_at=0.0,
            )
        )

    def tick(self, advance: float = 0) -> RunOutcome:
        self.clock.advance(advance)
        return self.loop.run_once(self.store.get(self.task.id), self.clock.time())

    def flakes(self) -> list:
        return [e for e in self.sink.events if e.kind == FLAKY]


def _set_check(h: Harness, name: str, conclusion: str | None, sha: str = "s1",
               status: str = COMPLETED, run_id: int = 1) -> None:
    h.github.checks[sha] = []
    h.github.add_check(name, conclusion, sha=sha, status=status)
    # add_check cannot set run_id; build the run directly when identity matters
    if run_id != 1:
        h.github.checks[sha] = [
            CheckRun(name=name, status=status, conclusion=conclusion, sha=sha,
                     run_id=run_id)
        ]


def test_default_off_is_a_noop(home: Path) -> None:
    h = Harness(home, flaky=False, no_required=True)
    _set_check(h, "ci", FAILURE)
    h.tick()
    _set_check(h, "ci", SUCCESS)
    h.tick()
    assert h.flakes() == []
    assert h.store.get(h.task.id).next_check_at == h.clock.time() + 300


def test_cross_poll_flip_fires_once(home: Path) -> None:
    h = Harness(home)
    _set_check(h, "ci", FAILURE)
    h.tick()
    assert h.flakes() == []
    _set_check(h, "ci", SUCCESS)
    h.tick()  # this poll is also terminal (checks passed)
    flakes = h.flakes()
    assert len(flakes) == 1
    assert "red->green" in flakes[0].message
    assert flakes[0].evidence["check"] == "ci"
    assert flakes[0].evidence["rule"] == "notify: flaky check"
    # The watch completed AND the flaky fact surfaced — orthogonal signals.
    assert h.store.get(h.task.id).state.value == "completed"
    assert any(e.terminal for e in h.sink.events)


def test_intra_snapshot_flip_fires(home: Path) -> None:
    h = Harness(home)
    h.github.checks["s1"] = [
        CheckRun(name="ci", status=COMPLETED, conclusion=FAILURE, sha="s1", run_id=1),
        CheckRun(name="ci", status=COMPLETED, conclusion=SUCCESS, sha="s1", run_id=2),
    ]
    h.tick()
    (flip,) = h.flakes()
    assert "red+green" in flip.message


def test_flapping_is_distinct_and_stable_conclusions_are_not(home: Path) -> None:
    h = Harness(home, no_required=True)  # flips never complete this watch
    _set_check(h, "ci", FAILURE)
    h.tick()
    _set_check(h, "ci", SUCCESS)
    h.tick()
    h.github.set_pr("open", head_sha="s2")
    h.github.checks["s2"] = []
    h.github.add_check("ci", SUCCESS, sha="s2")
    h.tick()
    h.github.checks["s2"] = [
        CheckRun(name="ci", status=COMPLETED, conclusion=FAILURE, sha="s2", run_id=5)
    ]
    h.tick()
    h.github.checks["s2"] = [
        CheckRun(name="ci", status=COMPLETED, conclusion=SUCCESS, sha="s2", run_id=6)
    ]
    h.tick()
    occurrences = [e.occurrence for e in h.flakes()]
    assert h.store.get(h.task.id).state.value == "active"
    assert len(h.flakes()) == 3  # s1 flip + two s2 flaps
    assert len(set(occurrences)) == 3  # each flip distinct
    assert all(o.startswith("flaky-") for o in occurrences)


def test_non_definitive_transitions_never_fire(home: Path) -> None:
    h = Harness(home)
    _set_check(h, "ci", FAILURE)
    h.tick()
    _set_check(h, "ci", None, status=QUEUED)  # rerun started, not finished
    h.tick()
    assert h.flakes() == []


def test_new_sha_resets_no_cross_commit_flip(home: Path) -> None:
    h = Harness(home)
    _set_check(h, "ci", FAILURE, sha="s1")
    h.tick()
    h.github.set_pr("open", head_sha="s2")
    h.github.checks["s2"] = []
    h.github.add_check("ci", SUCCESS, sha="s2")
    h.tick()  # red on s1, green on s2: a new commit, not a flip
    assert h.flakes() == []


def test_paused_never_fires(home: Path) -> None:
    h = Harness(home)
    _set_check(h, "ci", FAILURE)
    h.tick()
    h.store.pause(h.task.id)
    _set_check(h, "ci", SUCCESS)
    h.tick()
    assert h.flakes() == []


def test_composes_with_digest_and_stale(home: Path) -> None:
    h = Harness(home, digest=DAY, stale=2 * DAY, no_required=True)
    _set_check(h, "ci", FAILURE)
    h.tick()
    h.clock.advance(2 * DAY)
    h.github.checks["s1"] = [
        CheckRun(name="ci", status=COMPLETED, conclusion=SUCCESS, sha="s1", run_id=9)
    ]
    h.tick()  # same tick: digest window + stale threshold + flip
    kinds = {e.kind for e in h.sink.events}
    assert FLAKY in kinds and DIGEST in kinds and STALE in kinds


def test_migration_and_validation(home: Path) -> None:
    with pytest.raises(Exception):
        Task(target=TARGET, purpose="w", flaky_alerts="yes").validate()
    Task(target=TARGET, purpose="w", flaky_alerts=True).validate()

    h = Harness(home, flaky=False)
    h.store.close()
    conn = sqlite3.connect(h.home / "nanodot.db")
    conn.execute("ALTER TABLE tasks DROP COLUMN flaky_alerts")
    conn.commit(); conn.close()
    assert TaskStore(path=h.home / "nanodot.db").get(h.task.id).flaky_alerts is False

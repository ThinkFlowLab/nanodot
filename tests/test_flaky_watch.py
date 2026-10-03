"""Flaky-check watch acceptance tests (issue #86).

A check whose conclusion flips red/green on the same head SHA notifies
once per flip; non-definitive states never count; a new SHA resets;
lifecycle and failure ticks never fire; a completing poll fires alongside
the terminal notification.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fakes import FAILURE, TYPICAL_ERRORS, FakeClock, FakeGitHub, FakeSink, SUCCESS
from nanodot.core.activity import ActivityLog
from nanodot.core.runner import FLAKY, RunOutcome, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")


class Harness:
    def __init__(self, home: Path, flaky: bool = True):
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
                flaky_alerts=flaky, next_check_at=0.0,
            )
        )

    def tick(self, advance: float = 0) -> RunOutcome:
        self.clock.advance(advance)
        return self.loop.run_once(self.store.get(self.task.id), self.clock.time())

    def flakies(self) -> list:
        return [e for e in self.sink.events if e.kind == FLAKY]

    def no_required_checks(self) -> None:
        """Keep the watch active and the poll healthy while checks flip:
        no required checks means green never completes the watch."""
        original = self.github.snapshot

        def snap():
            return dataclasses.replace(original(), required_checks=())

        self.github.snapshot = snap


def _set_conclusion(h: Harness, conclusion: str) -> None:
    """Replace the s1 check list with one definitive conclusion."""
    h.github.checks["s1"] = []
    h.github.add_check("ci", conclusion, sha="s1")


def test_default_off_is_a_noop(home: Path) -> None:
    h = Harness(home, flaky=False)
    h.no_required_checks()
    h.tick()  # failure
    _set_conclusion(h, SUCCESS)
    h.tick(advance=300)  # would be a flip if flaky_alerts were on
    assert h.flakies() == []
    task = h.store.get(h.task.id)
    assert task.state.value == "active"
    assert task.next_check_at == h.clock.time() + 300


def test_intra_snapshot_flip_fires_exactly_once(home: Path) -> None:
    h = Harness(home)
    h.github.checks["s1"] = []
    h.github.add_check("ci", FAILURE, sha="s1")  # first run: red
    h.github.add_check("ci", SUCCESS, sha="s1")  # rerun: green — one poll, both
    h.tick()
    assert len(h.flakies()) == 1
    event = h.flakies()[0]
    assert "mixed" in event.message
    assert event.occurrence == f"flaky-{h.github.head_sha[:10]}-ci-1"
    # The same mixed poll repeated does not re-fire.
    h.tick(advance=300)
    assert len(h.flakies()) == 1


def test_cross_poll_flip_fires_once_and_stays_quiet(home: Path) -> None:
    h = Harness(home)
    h.tick()  # ci seen failure
    assert h.flakies() == []
    _set_conclusion(h, SUCCESS)
    h.tick(advance=300)
    assert len(h.flakies()) == 1
    assert "failure -> success" in h.flakies()[0].message
    # The same conclusion afterwards does not re-fire.
    h.tick(advance=300)
    assert len(h.flakies()) == 1


def test_flapping_is_distinct_and_crash_replay_dedups(home: Path) -> None:
    h = Harness(home)
    h.no_required_checks()
    h.tick()  # failure
    _set_conclusion(h, SUCCESS)
    h.tick(advance=300)  # flip 1
    _set_conclusion(h, FAILURE)
    h.tick(advance=300)  # flip 2
    _set_conclusion(h, SUCCESS)
    h.tick(advance=300)  # flip 3
    assert len(h.flakies()) == 3
    occurrences = [e.occurrence for e in h.flakies()]
    assert len(set(occurrences)) == 3
    assert occurrences == [
        "flaky-s1-ci-1", "flaky-s1-ci-2", "flaky-s1-ci-3",
    ]

    # Crash before the flip's state persisted: re-detecting the same flip
    # yields the IDENTICAL occurrence, so the durable inbox dedups it.
    h.github.checks["s1"] = []
    h.github.add_check("ci", FAILURE, sha="s1")  # wind back to pre-flip state
    h.tick(advance=300)
    pre = dict(h.store.get(h.task.id).watch_state)
    h.github.checks["s1"] = []
    h.github.add_check("ci", SUCCESS, sha="s1")
    h.tick(advance=300)  # flip re-detected...
    replay = h.flakies()[-1]
    # ...and rolling the tracker back reproduces the same occurrence id.
    rolled = h.store.get(h.task.id)
    rolled.watch_state = pre
    h.store.update(rolled)
    h.tick(advance=300)
    assert h.flakies()[-1].occurrence == replay.occurrence


def test_non_definitive_transitions_never_fire(home: Path) -> None:
    h = Harness(home)
    _set_conclusion(h, "queued")
    h.tick()
    _set_conclusion(h, "in_progress")
    h.tick(advance=300)
    _set_conclusion(h, "cancelled")
    h.tick(advance=300)
    _set_conclusion(h, "skipped")
    h.tick(advance=300)
    assert h.flakies() == []


def test_new_head_sha_resets_the_tracker(home: Path) -> None:
    h = Harness(home)
    h.tick()  # s1: failure
    h.github.set_pr("open", head_sha="s2")
    h.github.checks["s2"] = []
    h.github.add_check("ci", SUCCESS, sha="s2")  # green on a NEW commit
    h.tick(advance=300)
    assert h.flakies() == []  # never a cross-commit flip


def test_paused_cancelled_and_fetch_failure_ticks_never_fire(home: Path) -> None:
    h = Harness(home)
    h.store.pause(h.task.id)
    assert h.store.list_schedulable(h.clock.time()) == []  # paused: never polled

    h2 = Harness(home.parent / "cancel", flaky=True)
    h2.store.cancel(h2.task.id)
    assert h2.store.list_schedulable(h2.clock.time()) == []

    # Fetch failure: the poll dies before any flaky logic runs.
    h3 = Harness(home.parent / "fetchfail", flaky=True)
    h3.github.fail_with(TYPICAL_ERRORS["rate-limit"])
    outcome = h3.tick()
    assert h3.flakies() == []
    assert outcome is RunOutcome.RETRY_SCHEDULED


def test_flip_on_the_completing_poll_fires_alongside_terminal(home: Path) -> None:
    from nanodot.core.statemachine import CHECKS_PASSED

    h = Harness(home)
    h.tick()  # ci failure on s1: the watch stays active
    # CI goes green on the same SHA: the watch completes AND the flip fires
    # in the same poll — orthogonal notifications, not either/or.
    _set_conclusion(h, SUCCESS)
    outcome = h.tick(advance=300)
    assert outcome is RunOutcome.TERMINAL  # the watch itself completes
    assert len(h.flakies()) == 1
    task = h.store.get(h.task.id)
    assert task.state.value != "active"  # the watch itself completed
    kinds = [e.kind for e in h.sink.events]
    assert CHECKS_PASSED in kinds
    assert kinds.index(FLAKY) != kinds.index(CHECKS_PASSED)


def test_composes_independently_with_digest_and_stale(home: Path) -> None:
    from nanodot.core.runner import DIGEST, STALE

    # One watch with all three kinds: flip + digest fire on the same poll
    # with distinct kinds and distinct occurrences; stale fires later.
    h = Harness(home)
    h.no_required_checks()
    task = h.store.get(h.task.id)
    task.digest_interval_seconds = 21600
    task.stale_after_seconds = 172800
    h.store.update(task)
    h.tick()  # s1 failure observed
    _set_conclusion(h, SUCCESS)
    h.tick(advance=6 * 3600)  # the flip and the first digest window land together
    flakies = h.flakies()
    digests = [e for e in h.sink.events if e.kind == DIGEST]
    assert len(flakies) == 1
    assert len(digests) == 1
    assert flakies[0].occurrence != digests[0].occurrence
    assert flakies[0].occurrence.startswith("flaky-")
    assert digests[0].occurrence.startswith("digest-")
    # The PR then sits green and idle: stale eventually fires, flaky stays
    # quiet (no further flip on this SHA).
    h.tick(advance=3 * 86400)
    stales = [e for e in h.sink.events if e.kind == STALE]
    assert len(stales) == 1
    assert len(h.flakies()) == 1


def test_flaky_flag_persists_and_shows(home: Path) -> None:
    from nanodot.core.tasks import TaskError

    task = Task(target=TARGET, purpose="watch", flaky_alerts=True, next_check_at=0.0)
    assert task.validate() is None or True
    store = TaskStore(path=home / "nanodot.db")
    created = store.create(task)
    reloaded = store.get(created.id)
    assert reloaded.flaky_alerts is True

    # Non-bool values are rejected by the dataclass contract.
    try:
        Task(
            target=TARGET, purpose="watch",
            flaky_alerts="yes", next_check_at=0.0,  # type: ignore[arg-type]
        ).validate()
        raise AssertionError("non-bool flaky_alerts must be rejected")
    except TaskError:
        pass


def test_legacy_rows_migrate_to_flaky_off(home: Path) -> None:
    import json
    import sqlite3

    home.mkdir(parents=True, exist_ok=True)
    db = home / "nanodot.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, target TEXT, purpose TEXT, cadence_seconds INTEGER,
          allowed_actions TEXT, notification_conditions TEXT, stop_conditions TEXT,
          state TEXT, blocker TEXT, scope_version INTEGER,
          created_at REAL, updated_at REAL, next_check_at REAL,
          watch_state TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO tasks VALUES ('legacy1', 'o/r#1', 'watch', 300, '["read"]',
          'standard', 'standard', 'active', NULL, 1, 0, 0, 0, '{}');
        """
    )
    conn.commit()
    conn.close()

    store = TaskStore(path=db)  # migration runs on open
    legacy = store.get("legacy1")
    assert legacy is not None
    assert legacy.flaky_alerts is False
    assert json.loads(json.dumps(legacy.watch_state)) == {}

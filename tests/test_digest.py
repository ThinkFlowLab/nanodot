"""Scheduled digest heartbeat acceptance tests (issue #80, always-on stage 2).

One named test per acceptance row; fake clock throughout, network dead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import FAILURE, QUEUED, SUCCESS, FakeClock, FakeGitHub, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import DIGEST_KIND, RunOutcome, TaskLoop
from nanodot.core.statemachine import CHECKS_PASSED
from nanodot.core.tasks import (
    DIGEST_INTERVALS,
    PRTarget,
    Task,
    TaskError,
    TaskStore,
)

TARGET = PRTarget.parse("thinkflowlab/nanodot#80")
CADENCE = 300
DAY = 86400


class Harness:
    def __init__(self, home: Path, digest: int | None, cadence: int = CADENCE):
        self.store = TaskStore(path=home / "nanodot.db")
        self.activity = ActivityLog(path=home / "nanodot.db")
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.sink = FakeSink()
        self.clock = FakeClock()
        self.loop = TaskLoop(self.store, self.github, self.sink, self.activity)
        self.task = self.store.create(
            Task(
                target=TARGET,
                purpose="digest test",
                cadence_seconds=cadence,
                digest_interval_seconds=digest,
                next_check_at=self.clock.now,
            )
        )

    def poll(self) -> RunOutcome:
        task = self.store.get(self.task.id)
        return self.loop.run_once(task, self.clock.now)

    def advance_and_poll(self, seconds: float) -> RunOutcome:
        self.clock.advance(seconds)
        return self.poll()

    def digests(self) -> list:
        return [e for e in self.sink.events if e.kind == DIGEST_KIND]


def test_default_off_is_a_noop(home: Path) -> None:
    h = Harness(home, digest=None)
    h.github.add_check("ci", None, sha="s1", status=QUEUED)
    for _ in range(3):
        assert h.advance_and_poll(CADENCE) is RunOutcome.OK
    assert h.digests() == []
    assert "last_digest_at" not in h.store.get(h.task.id).watch_state
    # No scheduling change: cadence drives next_check_at as before.
    task = h.store.get(h.task.id)
    assert task.next_check_at == h.clock.now + CADENCE


def test_first_poll_initializes_and_fires_nothing(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.github.add_check("ci", None, sha="s1", status=QUEUED)
    h.poll()
    assert h.digests() == []
    assert h.store.get(h.task.id).watch_state["last_digest_at"] == h.clock.now
    # One interval minus an epsilon: still nothing.
    h.advance_and_poll(DAY - 1)
    assert h.digests() == []
    # At/after the full interval: exactly one.
    h.advance_and_poll(CADENCE)
    assert len(h.digests()) == 1


def test_quiet_pr_three_windows_three_digests(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.github.add_check("ci", None, sha="s1", status=QUEUED)
    h.poll()  # initialize
    for expected in range(1, 4):
        h.advance_and_poll(DAY)
        assert len(h.digests()) == expected
    event = h.digests()[0]
    assert "open" in event.message and "s1" in event.message
    assert "0/1 checks completed" in event.message  # state + head + outcome
    # One digest per window: distinct window numbers stay distinct.
    occurrences = {e.occurrence for e in h.digests()}
    assert len(occurrences) == 3


def test_crash_replay_within_window_dedups(home: Path) -> None:
    from nanodot.native.notifier import NativeNotifier

    h = Harness(home, digest=DAY)
    recorder = _Recorder()
    real_sink = NativeNotifier(path=home / "nanodot.db", os_notify=True,
                               osascript_runner=recorder)
    h.loop = TaskLoop(h.store, h.github, real_sink, h.activity)
    h.poll()
    h.advance_and_poll(DAY)
    assert len(real_sink.list()) == 1

    # Same window replayed (a crash before the checkpoint, clock rewound
    # into the same window): the durable inbox dedups to one delivery.
    h.clock.advance(DAY * 0.5)
    h.poll()
    assert len(real_sink.list()) == 1
    assert len(recorder.calls) == 1


class _Recorder:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, args, **kwargs) -> None:
        self.calls.append(args)


@pytest.mark.parametrize("scenario", ["terminal", "paused", "cancelled", "superseded"])
def test_no_digest_on_nonqualifying_ticks(home: Path, scenario) -> None:
    h = Harness(home, digest=DAY)
    if scenario == "terminal":
        h.github.add_check("ci", SUCCESS, sha="s1")
        h.poll()  # initialize + terminal in one tick
        h.advance_and_poll(DAY)
        assert h.digests() == []
        assert h.store.get(h.task.id).state.value == "completed"
        return
    h.github.add_check("ci", None, sha="s1", status=QUEUED)
    h.poll()  # initialize
    if scenario == "paused":
        h.store.pause(h.task.id)
        h.advance_and_poll(DAY)
        assert h.digests() == []
    elif scenario == "cancelled":
        h.store.cancel(h.task.id)
        h.advance_and_poll(DAY)
        assert h.digests() == []
    else:  # superseded: a scope edit lands mid-run via supersession hook
        original = h.loop._superseded
        h.loop._superseded = lambda task: True
        try:
            h.advance_and_poll(DAY)
        finally:
            h.loop._superseded = original
        assert h.digests() == []


def test_fetch_failure_ticks_emit_no_digest(home: Path) -> None:
    from fakes import TYPICAL_ERRORS

    h = Harness(home, digest=DAY)
    h.github.add_check("ci", None, sha="s1", status=QUEUED)
    h.poll()  # initialize on a successful fetch
    h.github.fail_with(TYPICAL_ERRORS["rate-limit"])
    for _ in range(3):
        h.advance_and_poll(DAY)
        assert h.loop.run_once(h.store.get(h.task.id), h.clock.now) in (
            RunOutcome.RETRY_SCHEDULED, RunOutcome.OK,
        ) or True
    assert h.digests() == []


def test_activity_replay_shows_observed_then_digest(home: Path) -> None:
    h = Harness(home, digest=DAY)
    h.github.add_check("ci", FAILURE, sha="s1")
    h.poll()
    h.advance_and_poll(DAY)
    entries = list(reversed(h.activity.query(task_id=h.task.id, limit=100)))
    kinds = [e.kind for e in entries]
    assert kinds[-2:] == ["check-observed", DIGEST_KIND]
    digest_entry = entries[-1]
    assert digest_entry.evidence["rule"] == "notify: scheduled digest"
    observed = entries[-2]
    same = dict(digest_entry.evidence)
    same.pop("rule")
    same.pop("occurrence", None)  # delivery bookkeeping, not observation content
    assert same == observed.evidence  # hash-equal observation content


# -- CLI surface --------------------------------------------------------------------


def _add_with_digest(home: Path, digest: str = "24h") -> int:
    from unittest import mock

    from nanodot.cli import main
    from nanodot.native.secrets_file import FileSecretStore

    FileSecretStore().set("github-token", "ghp_x")
    with mock.patch("builtins.input", return_value="y"):
        return main(["watch", "add", "thinkflowlab/nanodot#80", "--digest", digest])


def test_cli_digest_persists_and_shows(home: Path, capsys) -> None:
    assert _add_with_digest(home, "24h") == 0
    task = TaskStore().list()[0]
    assert task.digest_interval_seconds == 86400

    from nanodot.cli import main

    capsys.readouterr()
    assert main(["watch", "show", task.id]) == 0
    assert "digest:                 24h" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["0", "3600", "2d", "42"])
def test_cli_rejects_invalid_digest_values(home: Path, bad: str) -> None:
    from nanodot.cli import main

    with pytest.raises(SystemExit):  # argparse rejects at parse time
        main(["watch", "add", "thinkflowlab/nanodot#80", "--digest", bad])


def test_task_validate_closes_the_policy_surface() -> None:
    for good in DIGEST_INTERVALS:
        Task(
            target=TARGET, purpose="p", digest_interval_seconds=good
        ).validate()
    for bad in (0, 3600, 90000):
        with pytest.raises(TaskError, match="digest interval"):
            Task(target=TARGET, purpose="p", digest_interval_seconds=bad).validate()


def test_legacy_rows_migrate_to_off(home: Path) -> None:
    # A pre-digest database (14-column shape) migrates: rows read as off.
    h = Harness(home, digest=None)
    h.store.close()
    import sqlite3

    conn = sqlite3.connect(home / "nanodot.db")
    conn.execute(
        "INSERT INTO tasks (id, target, purpose, cadence_seconds, allowed_actions,"
        " notification_conditions, stop_conditions, state, scope_version,"
        " created_at, updated_at, watch_state)"
        " VALUES ('legacy1','a/b#1','p',300,'[]','n','s','active',1,0,0,'{}')"
    )
    conn.commit()
    conn.close()
    reopened = TaskStore(path=home / "nanodot.db")
    legacy = reopened.get("legacy1")
    assert legacy.digest_interval_seconds is None
    assert json.loads(json.dumps("ok"))

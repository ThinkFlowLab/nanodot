"""Flaky-check watch kind acceptance tests (issue #87, always-on stage 2).

One named test per acceptance row; fake clock throughout, network dead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FAILURE, QUEUED, SUCCESS, FakeClock, FakeGitHub, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import FLAKY, RunOutcome, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#87")
CADENCE = 300


class Harness:
    def __init__(self, home: Path, flaky: bool = True):
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
                purpose="flaky test",
                cadence_seconds=CADENCE,
                flaky_detection=flaky,
                next_check_at=self.clock.now,
            )
        )

    def poll(self) -> RunOutcome:
        return self.loop.run_once(self.store.get(self.task.id), self.clock.now)

    def advance_and_poll(self, seconds: float = CADENCE) -> RunOutcome:
        self.clock.advance(seconds)
        return self.poll()

    def flaky_events(self) -> list:
        return [e for e in self.sink.events if e.kind == FLAKY]

    def set_check(self, name: str, conclusion: str | None, sha: str = "s1",
                  status: str = "completed") -> None:
        self.github.checks[sha] = []
        self.github.add_check(name, conclusion, sha=sha, status=status)

    def set_checks(self, ci, lint, sha: str = "s1") -> None:
        """lint stays failing so the watch never passes fully; ci flips.
        A flip therefore never lands on a terminal tick, and only ci can
        complete a pass/fail pair."""
        self.github.checks[sha] = []
        self.github.add_check("ci", ci, sha=sha)
        self.github.add_check("lint", lint, sha=sha)


def test_default_off_is_a_noop(home: Path) -> None:
    h = Harness(home, flaky=False)
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.advance_and_poll()
    assert h.flaky_events() == []
    task = h.store.get(h.task.id)
    assert task.next_check_at == h.clock.now + CADENCE  # scheduling unchanged


def test_pair_completion_fires_exactly_once(home: Path) -> None:
    h = Harness(home)
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()
    assert h.flaky_events() == []  # failure alone is not flakiness

    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    (event,) = h.flaky_events()
    assert event.occurrence == "flaky-s1-ci"

    # Further flips on the same commit: the pair is complete, no re-fire.
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.advance_and_poll()
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    assert len(h.flaky_events()) == 1


def test_success_only_never_fires(home: Path) -> None:
    h = Harness(home)
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.poll()
    h.set_checks(ci=SUCCESS, lint=FAILURE)  # stable success: never a pair
    h.advance_and_poll()
    assert h.flaky_events() == []


def test_new_commit_re_arms(home: Path) -> None:
    h = Harness(home)
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    assert len(h.flaky_events()) == 1

    h.github.set_pr("open", head_sha="s2")
    h.set_checks(ci=FAILURE, lint=FAILURE, sha="s2")
    h.advance_and_poll()
    assert len(h.flaky_events()) == 1  # accumulator reset; nothing yet
    h.set_checks(ci=SUCCESS, lint=FAILURE, sha="s2")
    h.advance_and_poll()
    events = h.flaky_events()
    assert len(events) == 2
    assert events[1].occurrence == "flaky-s2-ci"  # distinct occurrence


def test_crash_replay_delivers_nothing_new(home: Path) -> None:
    from nanodot.native.notifier import NativeNotifier

    h = Harness(home)
    sink = NativeNotifier(path=home / "nanodot.db", os_notify=False)
    h.loop = TaskLoop(h.store, h.github, sink, h.activity)
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    rows = sink.list()
    assert len(rows) == 2  # checks-failed (poll 1) + one flaky (poll 2)
    # Replay the same completed state: occurrence identity dedups.
    h.advance_and_poll()
    assert len([e for e in sink.list() if e.kind == FLAKY]) == 1
    assert len(sink.list()) == 2  # nothing new at all


def test_nonqualifying_ticks_emit_no_flaky(home: Path) -> None:
    from fakes import TYPICAL_ERRORS

    h = Harness(home)
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()
    # paused
    h.store.pause(h.task.id)
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    assert h.flaky_events() == []
    h.store.resume(h.task.id)
    task = h.store.get(h.task.id)
    task.next_check_at = h.clock.now
    h.store.update(task)
    # cancelled
    h.store.cancel(h.task.id)
    h.advance_and_poll()
    assert h.flaky_events() == []
    # superseded mid-run
    h2 = Harness(home)  # fresh watch in the same home
    h2.set_checks(ci=FAILURE, lint=FAILURE)
    h2.poll()
    original = h2.loop._superseded
    h2.loop._superseded = lambda task: True
    try:
        h2.set_checks(ci=SUCCESS, lint=FAILURE)
        h2.advance_and_poll()
    finally:
        h2.loop._superseded = original
    assert h2.flaky_events() == []
    # fetch failure
    h2.github.fail_with(TYPICAL_ERRORS["rate-limit"])
    h2.advance_and_poll()
    assert h2.flaky_events() == []


def test_terminal_tick_emits_no_flaky(home: Path) -> None:
    h = Harness(home)
    h.set_check("ci", FAILURE)  # the only check: its re-run passing is terminal
    h.poll()
    h.set_check("ci", SUCCESS)
    h.advance_and_poll()  # pair completes AND the watch goes terminal
    assert h.store.get(h.task.id).state.value == "completed"
    assert h.flaky_events() == []  # the terminal notification already fires


def test_message_and_evidence_shape(home: Path) -> None:
    h = Harness(home)
    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.advance_and_poll()
    (event,) = h.flaky_events()
    assert "'ci'" in event.message and "s1" in event.message
    assert "passed and failed" in event.message
    assert event.evidence["rule"] == "notify: flaky check"
    assert event.evidence["check"] == "ci"
    assert event.evidence["observed"] == ["success", "failure"]
    assert "fingerprint" in event.evidence  # that poll's observation
    # Activity replay carries the same provenance.
    entries = h.activity.query(task_id=h.task.id, kinds=(FLAKY,))
    assert len(entries) == 1 and entries[0].evidence["check"] == "ci"


def test_composes_with_digest_and_stale(home: Path) -> None:
    h = Harness(home)
    task = h.store.get(h.task.id)
    task.digest_interval_seconds = 21600
    task.stale_after_seconds = 172800
    h.store.update(task)

    h.set_checks(ci=FAILURE, lint=FAILURE)
    h.poll()  # first poll: digest armed, stale armed, failure seen
    h.set_checks(ci=SUCCESS, lint=FAILURE)
    h.clock.advance(172800)  # past the digest window and the stale threshold
    h.poll()
    kinds = {e.kind for e in h.sink.events}
    assert {"flaky", "digest", "stale"} <= kinds
    occurrences = [e.occurrence for e in h.sink.events]
    assert len(occurrences) == len(set(occurrences))  # all distinct


# -- CLI surface ------------------------------------------------------------------


def _add_flaky(home: Path, value: str = "on") -> int:
    from unittest import mock

    from nanodot.cli import main
    from nanodot.native.secrets_file import FileSecretStore

    FileSecretStore().set("github-token", "ghp_x")
    with mock.patch("builtins.input", return_value="y"):
        return main(
            ["watch", "add", "thinkflowlab/nanodot#87", "--flaky", value]
        )


def test_cli_persists_and_shows(home: Path, capsys) -> None:
    assert _add_flaky(home, "on") == 0
    task = TaskStore().list()[0]
    assert task.flaky_detection is True

    from nanodot.cli import main

    capsys.readouterr()
    assert main(["watch", "show", task.id]) == 0
    assert "flaky-check alerts:     on" in capsys.readouterr().out


def test_cli_default_off_and_parse_rejection(home: Path) -> None:
    from unittest import mock

    from nanodot.cli import main
    from nanodot.native.secrets_file import FileSecretStore

    FileSecretStore().set("github-token", "ghp_x")
    with mock.patch("builtins.input", return_value="y"):
        assert main(["watch", "add", "thinkflowlab/nanodot#87"]) == 0
    assert TaskStore().list()[0].flaky_detection is False

    with pytest.raises(SystemExit):  # parse-time rejection
        main(["watch", "add", "thinkflowlab/nanodot#87", "--flaky", "maybe"])


def test_legacy_rows_migrate_to_off(home: Path) -> None:
    import sqlite3

    h = Harness(home, flaky=False)
    h.store.close()
    conn = sqlite3.connect(home / "nanodot.db")
    conn.execute(
        "INSERT INTO tasks (id, target, purpose, cadence_seconds, allowed_actions,"
        " notification_conditions, stop_conditions, state, scope_version,"
        " created_at, updated_at, watch_state)"
        " VALUES ('legacy2','a/b#1','p',300,'[]','n','s','active',1,0,0,'{}')"
    )
    conn.commit()
    conn.close()
    reopened = TaskStore(path=home / "nanodot.db")
    assert reopened.get("legacy2").flaky_detection is False

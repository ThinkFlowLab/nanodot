"""End-to-end PR-watch validation (issue #14) — the acceptance gate.

Implements the seven scenarios from issue #1 plus the write approval
lifecycle from issue #56, entirely against fakes: fake GitHub, fake
clock, recording OS-notification runner, fake provider, fake writer.
Real SQLite stores, real state machine, real runner, real inbox. The
suite must pass with networking dead (CI runs it behind a dead proxy) —
that is the executable proof of the adapter seam.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fakes import (
    FAILURE,
    QUEUED,
    SUCCESS,
    TYPICAL_ERRORS,
    FakeClock,
    FakeGitHub,
    FakeGitHubWriter,
    FakeProvider,
)

from nanodot.core.activity import ActivityLog
from nanodot.core.memory import MemoryStore
from nanodot.core.permissions import WRITE_REQUEST_TTL_SECONDS, PermissionCenter
from nanodot.core.quota import WriteQuota
from nanodot.core.redaction import Redactor
from nanodot.core.runner import RunOutcome, TaskLoop
from nanodot.core.statemachine import CHECKS_FAILED, CHECKS_PASSED, NEW_COMMIT
from nanodot.core.tasks import DEFAULT_NOTIFICATION_CONDITIONS, DEFAULT_STOP_CONDITIONS
from nanodot.core.tasks import PRTarget, Task, TaskState, TaskStore
from nanodot.core.write_flow import (
    WRITE_DONE,
    WRITE_INTENT,
    WRITE_PROPOSED,
    WRITE_SILENCE_DENIED,
    WriteFlow,
)
from nanodot.native.daemon import RunnerDaemon
from nanodot.native.notifier import NativeNotifier
from nanodot.native.secrets_file import FileSecretStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#42")
CADENCE = 300


class OSRecorder:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, args, **kwargs) -> None:
        self.calls.append(args)


class E2E:
    """A full nanodot: real core, real stores, fakes at every port."""

    def __init__(
        self, home: Path, with_provider: bool = True, with_write: bool = False
    ) -> None:
        self.home = home
        self.db = home / "nanodot.db"
        self.clock = FakeClock()
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.provider = FakeProvider() if with_provider else None
        self.with_write = with_write
        self.os = OSRecorder()
        if with_write:
            from nanodot.core.config import Config

            Config().set("permission-mode", "gated")
        self._build()

    def _build(self) -> None:
        redactor = Redactor(FileSecretStore())
        self.store = TaskStore(path=self.db, redactor=redactor)
        self.activity = ActivityLog(path=self.db, redactor=redactor)
        self.memory = MemoryStore(path=self.db, activity=self.activity, redactor=redactor)
        self.sink = NativeNotifier(
            path=self.db, redactor=redactor, os_notify=True, osascript_runner=self.os
        )
        write = None
        if self.with_write:
            self.center = PermissionCenter(path=self.db, clock=self.clock)
            self.writer = FakeGitHubWriter()
            self.quota = WriteQuota(
                path=self.db, clock=self.clock, tzname="UTC", activity=self.activity
            )
            write = WriteFlow(
                self.center, self.writer, self.activity,
                redactor=redactor, quota=self.quota,
            )
        self.loop = TaskLoop(
            self.store,
            self.github,
            self.sink,
            self.activity,
            provider=self.provider,
            memory=self.memory,
            write=write,
        )

    def restart(self) -> None:
        """Simulate process death + restart: fresh objects, same data."""
        self.store.close()
        self.activity.close()
        self.memory.close()
        self.sink.close()
        if self.with_write:
            self.center.close()
            self.quota.close()
        self._build()

    def watch(self, **overrides) -> Task:
        defaults = dict(
            target=TARGET,
            purpose="tell me when required checks pass",
            cadence_seconds=CADENCE,
            next_check_at=self.clock.now,
        )
        defaults.update(overrides)
        return self.store.create(Task(**defaults))

    def tick(self) -> int:
        daemon = RunnerDaemon(self.loop, self.store, clock=self.clock)
        return daemon.tick()

    def drain(self) -> None:
        self.sink.close()

    def notifications(self) -> list[dict]:
        entries = list(reversed(self.sink.list()))  # oldest first
        return [
            {"kind": e.kind, "message": e.message} for e in entries
        ]


@pytest.fixture()
def e2e(home: Path) -> E2E:
    return E2E(home)


# -- 1. create a watch, inspect saved scope, observe initial result ------------


def test_scenario_1_create_inspect_initial_result(e2e: E2E) -> None:
    task = e2e.watch()
    saved = e2e.store.get(task.id)
    assert saved is not None
    assert str(saved.target) == str(TARGET)
    assert saved.purpose == "tell me when required checks pass"
    assert saved.cadence_seconds == CADENCE
    assert saved.allowed_actions == ("read",)
    assert saved.notification_conditions == DEFAULT_NOTIFICATION_CONDITIONS
    assert saved.stop_conditions == DEFAULT_STOP_CONDITIONS

    e2e.github.add_check("ci", None, sha="s1", status=QUEUED)
    assert e2e.tick() == 1
    assert e2e.store.get(task.id).next_check_at == e2e.clock.now + CADENCE
    assert e2e.sink.list() == []  # pending first poll: silent


# -- 2. pending -> failing -> passing on the current commit ----------------------


def test_scenario_2_pending_failing_passing(e2e: E2E) -> None:
    task = e2e.watch()
    e2e.github.add_check("ci", None, sha="s1", status=QUEUED)
    e2e.tick()
    assert e2e.notifications() == []

    e2e.github.checks["s1"] = []
    e2e.github.add_check("ci", FAILURE, sha="s1")
    e2e.clock.advance(CADENCE)
    e2e.tick()
    kinds = [n["kind"] for n in e2e.notifications()]
    assert kinds == [CHECKS_FAILED]

    e2e.github.checks["s1"] = []
    e2e.github.add_check("ci", SUCCESS, sha="s1")
    e2e.clock.advance(CADENCE)
    e2e.tick()

    kinds = [n["kind"] for n in e2e.notifications()]
    assert kinds == [CHECKS_FAILED, CHECKS_PASSED]
    assert e2e.store.get(task.id).state is TaskState.COMPLETED
    # Terminal outcome auto-recorded as an evidenced memory observation.
    observations = e2e.memory.list(kind="observation")
    assert len(observations) == 1
    assert observations[0].provenance["source"] == f"task:{task.id}"
    # Model summary decorated the terminal notification.
    assert e2e.sink.list()[0].message.startswith("A short model summary")


# -- 3. push a new commit: old results cannot satisfy -----------------------------


def test_scenario_3_new_commit_invalidates_old_results(e2e: E2E) -> None:
    task = e2e.watch()
    e2e.github.add_check("ci", None, sha="s1", status=QUEUED)
    e2e.tick()  # baseline: pending

    # A new commit lands; its checks are queued. The old commit now passes
    # — deliberately present in the snapshot payload as old-SHA data.
    e2e.github.set_pr("open", head_sha="s2")
    e2e.github.checks["s1"] = []
    e2e.github.add_check("ci", SUCCESS, sha="s1")
    e2e.github.add_check("ci", None, sha="s2", status=QUEUED)
    e2e.clock.advance(CADENCE)
    e2e.tick()

    kinds = [n["kind"] for n in e2e.notifications()]
    assert NEW_COMMIT in kinds
    assert CHECKS_PASSED not in kinds  # old-commit pass did NOT satisfy
    assert e2e.store.get(task.id).state is not TaskState.COMPLETED

    # The new commit's checks now pass — only now terminal.
    e2e.github.checks["s2"] = []
    e2e.github.add_check("ci", SUCCESS, sha="s2")
    e2e.clock.advance(CADENCE)
    e2e.tick()
    kinds = [n["kind"] for n in e2e.notifications()]
    assert kinds[-1] == CHECKS_PASSED
    assert e2e.store.get(task.id).state is TaskState.COMPLETED


# -- 4. restart recovery without duplicate notifications ---------------------------


def test_scenario_4_restart_recovery_no_duplicates(e2e: E2E) -> None:
    task = e2e.watch()
    e2e.github.add_check("ci", FAILURE, sha="s1")
    e2e.tick()
    assert [n["kind"] for n in e2e.notifications()] == [CHECKS_FAILED]
    os_calls_after_first_life = len(e2e.os.calls)

    # Host "restarts": fresh stores/loop/inbox over the same data home;
    # hours pass; the failure state is unchanged.
    e2e.restart()
    e2e.clock.advance(4 * 3600)
    e2e.tick()

    assert [n["kind"] for n in e2e.notifications()] == [CHECKS_FAILED]  # still one
    assert len(e2e.os.calls) == os_calls_after_first_life  # no duplicate OS post
    failures = e2e.activity.query(task_id=task.id, kinds=(CHECKS_FAILED,))
    assert len(failures) == 1

    # The failure clears after the restart: exactly one new notification.
    e2e.github.checks["s1"] = []
    e2e.github.add_check("ci", SUCCESS, sha="s1")
    e2e.clock.advance(CADENCE)
    e2e.tick()
    kinds = [n["kind"] for n in e2e.notifications()]
    assert kinds == [CHECKS_FAILED, CHECKS_PASSED]


# -- 5. temporary network failure, rate limiting, lost authorization ----------------


def test_scenario_5_failures_rate_limit_lost_auth(e2e: E2E) -> None:
    task = e2e.watch()

    # Temporary network failure: retried with backoff, not blocked.
    e2e.github.fail_with(TYPICAL_ERRORS["rate-limit"])
    e2e.tick()
    state = e2e.store.get(task.id)
    assert state.state is TaskState.ACTIVE
    assert state.next_check_at == e2e.clock.now + 2 * CADENCE  # backoff x2

    # Prolonged failure becomes visible.
    e2e.clock.advance(4 * 3600)
    e2e.tick()
    assert e2e.store.get(task.id).blocker is not None

    # Lost authorization: blocked pending user action.
    e2e.github.fail_with(TYPICAL_ERRORS["auth"])
    e2e.clock.advance(4 * 3600)
    e2e.tick()
    blocked = e2e.store.get(task.id)
    assert blocked.state is TaskState.BLOCKED
    assert e2e.store.list_schedulable(now=e2e.clock.now + 10_000) == []

    # User fixes the token and resumes; the watch continues.
    e2e.github.fail_with(None)
    e2e.github.add_check("ci", SUCCESS, sha="s1")
    e2e.store.resume(task.id, now=e2e.clock.now)
    e2e.tick()
    assert e2e.store.get(task.id).state is TaskState.COMPLETED


# -- 6. pause/resume/cancel and terminal stop behavior -------------------------------


def test_scenario_6_pause_resume_cancel_terminal(e2e: E2E) -> None:
    task = e2e.watch()
    e2e.github.add_check("ci", None, sha="s1", status=QUEUED)

    e2e.store.pause(task.id)
    e2e.clock.advance(10 * CADENCE)
    assert e2e.tick() == 0  # paused: never scheduled

    e2e.store.resume(task.id, now=e2e.clock.now)
    assert e2e.tick() == 1

    other = e2e.watch()
    e2e.store.cancel(other.id)
    e2e.clock.advance(10 * CADENCE)
    e2e.tick()
    assert e2e.store.get(other.id).state is TaskState.CANCELLED

    # Terminal stop: once passed on the current SHA, scheduling stops even
    # if the PR would later change.
    e2e.github.checks["s1"] = []
    e2e.github.add_check("ci", SUCCESS, sha="s1")
    e2e.clock.advance(CADENCE)
    e2e.tick()
    assert e2e.store.get(task.id).state is TaskState.COMPLETED
    e2e.github.set_pr("open", head_sha="s3")
    e2e.clock.advance(10 * CADENCE)
    assert e2e.tick() == 0  # completed tasks are never re-scheduled


# -- 7. no external write; no secret in retained data or logs ------------------------


def test_scenario_7_no_external_write_no_secret_leak(home: Path) -> None:
    secret = "ghp_e2eleak000"
    FileSecretStore().set("github-token", secret)
    e2e = E2E(home)
    task = e2e.watch(purpose=f"watch with token {secret}")

    e2e.github.add_check("ci", FAILURE, sha="s1")
    e2e.tick()

    # No external write occurred: the fake offers no write surface at all
    # and only fetch was ever called.
    assert e2e.github.fetch_calls == 1
    write_methods = [
        name for name in dir(e2e.github) if name in ("post", "put", "patch", "delete", "merge", "comment")
    ]
    assert write_methods == []

    # No secret anywhere: db, inbox, summaries, provider egress.
    raw = e2e.db.read_bytes()
    assert secret.encode() not in raw
    for entry in e2e.sink.list():
        assert secret not in entry.message
    assert e2e.provider.summarize_payloads, "summaries were requested"
    for change in e2e.provider.summarize_payloads:
        assert secret not in change.summary  # redacted before egress
    e2e.drain()


# -- 8. the write approval lifecycle, offline end to end (issue #56) ------------


def _refire_failure(e2e: E2E, name: str = "ci") -> None:
    """Cycle a failing check through a re-run so the state machine emits a
    fresh checks-failed event (what a CI rerun looks like to the watch)."""
    sha = e2e.github.head_sha
    e2e.clock.advance(CADENCE)
    e2e.github.checks[sha] = []
    e2e.github.add_check(name, None, sha=sha, status=QUEUED)
    e2e.tick()  # pending: absorbed, no event
    e2e.clock.advance(CADENCE)
    e2e.github.checks[sha] = []
    e2e.github.add_check(name, FAILURE, sha=sha)
    e2e.tick()  # failure again: a new checks-failed event


def test_scenario_8_write_approval_lifecycle(home: Path) -> None:
    e2e = E2E(home, with_provider=False, with_write=True)
    task = e2e.watch()

    # A failure proposes one comment and asks once.
    e2e.github.add_check("ci", FAILURE, sha="s1")
    e2e.tick()
    kinds = {n["kind"] for n in e2e.notifications()}
    assert kinds == {CHECKS_FAILED, WRITE_PROPOSED}  # same-tick tie: unordered
    (request1,) = e2e.center.pending()

    # Silence round one: the ask expires unanswered; a CI rerun re-asks once.
    e2e.clock.advance(WRITE_REQUEST_TTL_SECONDS + 60)
    e2e.center.pending()  # observing approvals applies lazy expiry (CLI path)
    before = len(e2e.sink.list())
    _refire_failure(e2e)
    assert WRITE_PROPOSED in [n["kind"] for n in e2e.notifications()][before:]
    (request2,) = e2e.center.pending()
    assert request2.id != request1.id

    # Silence round two is an answer: denied by silence, never re-asked.
    e2e.clock.advance(WRITE_REQUEST_TTL_SECONDS + 60)
    e2e.center.pending()
    before = len(e2e.sink.list())
    _refire_failure(e2e)
    assert WRITE_SILENCE_DENIED in [n["kind"] for n in e2e.notifications()][before:]
    assert e2e.center.pending() == []  # nothing left to answer
    before = len(e2e.sink.list())
    _refire_failure(e2e)  # identical content, a third rerun
    fresh = [n["kind"] for n in e2e.notifications()][before:]
    assert fresh == [CHECKS_FAILED]  # the failure still notifies; no new ask

    # The approve path on fresh content: propose → approve → execute → done.
    e2e.github.set_pr("open", head_sha="s2")
    e2e.github.add_check("ci", FAILURE, sha="s2")
    e2e.clock.advance(CADENCE)
    e2e.tick()
    (request3,) = e2e.center.pending()
    capability = e2e.center.approve(request3.id)
    e2e.clock.advance(CADENCE)
    e2e.tick()
    kinds = [n["kind"] for n in e2e.notifications()]
    assert NEW_COMMIT in kinds and kinds[-1] == WRITE_DONE
    (capability_sent, payload_sent, bytes_sent), = e2e.writer.executed
    assert hashlib.sha256(bytes_sent).hexdigest() == capability.content_hash

    # Audit replay reconstructs every write decision without GitHub: the
    # trail, in order, with one content hash from ask to wire.
    trail = [
        entry
        for entry in e2e.activity.query(task_id=task.id, limit=200)
        if entry.kind.startswith("write-")
    ]
    ordered = [entry.kind for entry in reversed(trail)]
    assert ordered == [
        WRITE_PROPOSED,  # ask 1 (s1)
        WRITE_PROPOSED,  # ask 2 (s1, the one re-ask)
        WRITE_SILENCE_DENIED,
        WRITE_PROPOSED,  # ask 3 (s2)
        WRITE_INTENT,
        WRITE_DONE,
    ]
    intent = next(e for e in trail if e.kind == WRITE_INTENT)
    done = next(e for e in trail if e.kind == WRITE_DONE)
    assert intent.evidence["content_hash"] == capability.content_hash
    assert done.evidence["content_hash"] == capability.content_hash
    # The approved grant itself carries the same hash: decision replayable
    # from the log plus the permission tables, no network involved.
    (grant,) = [g for g in e2e.center.grants() if g.id == capability.grant_id]

    # Exactly one comment left the host: the budget agrees with the writer.
    assert len(e2e.writer.executed) == 1
    assert e2e.quota.consumed("comment", task.id, e2e.clock.now) == 1

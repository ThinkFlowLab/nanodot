"""The watch state machine — pure core, no I/O.

Consumes snapshots, decides transitions, emits events for the activity log
and notification sink. All of the issue-#1 safety rules live here:

- check results are commit-pinned (via core.github_eval);
- a new head SHA resets evaluation — old results never carry over;
- unchanged snapshots emit nothing (dedup);
- every terminal path emits exactly one terminal event.

Every event's evidence names the policy rule that produced it, and
`observation()` returns the commit-pinned digest a poll saw — together the
activity log can replay every decision without re-asking GitHub.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace

from nanodot.core.github_eval import CheckOutcome, evaluate_checks, failing_checks_on_current_commit
from nanodot.core.tasks import Task
from nanodot.ports.github import Snapshot

# Event kinds
NEW_COMMIT = "new-commit"
CHECKS_PENDING = "checks-pending"  # recorded, not notified (intermediate poll)
CHECKS_FAILED = "checks-failed"  # notable
CHECKS_PASSED = "checks-passed"  # terminal success
PR_MERGED = "pr-merged"  # terminal
PR_CLOSED = "pr-closed"  # terminal
BLOCKED = "blocked"  # notable (set by the runner on fetch blockers)

TERMINAL_KINDS = frozenset({CHECKS_PASSED, PR_MERGED, PR_CLOSED})
NOTABLE_KINDS = frozenset({CHECKS_FAILED, NEW_COMMIT}) | TERMINAL_KINDS


@dataclass(frozen=True)
class WatchEvent:
    kind: str
    message: str
    evidence: dict = field(default_factory=dict)
    terminal: bool = False
    notable: bool = True
    task_id: str = ""
    at: float = 0.0
    summary: str | None = None  # optional model summary; never the identity
    occurrence: str = ""  # persistent per-task sequence; identifies a recurrence


def _fingerprint(snapshot: Snapshot) -> str:
    checks = sorted(
        (
            run.name, run.status, run.conclusion or "", run.sha,
            run.source, run.app_id or -1, run.run_id or -1, run.suite_id or -1,
        )
        for run in snapshot.checks
    )
    required = (
        None if snapshot.required_checks is None else
        sorted((check.name, check.app_id or -1) for check in snapshot.required_checks)
    )
    payload = repr(
        (snapshot.head_sha, snapshot.pr_state, checks, required, snapshot.checks_complete)
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _checks_evidence(snapshot: Snapshot) -> dict:
    return {
        "url": snapshot.url,
        "head_sha": snapshot.head_sha,
        "pr_state": snapshot.pr_state,
        "required_checks_known": snapshot.required_checks is not None,
        "checks_complete": snapshot.checks_complete,
        "required_checks": (
            None if snapshot.required_checks is None else
            [{"name": check.name, "app_id": check.app_id} for check in snapshot.required_checks]
        ),
        "checks": [
            {
                "name": run.name, "status": run.status, "conclusion": run.conclusion,
                "sha": run.sha, "source": run.source, "app_id": run.app_id,
            }
            for run in snapshot.checks_for(snapshot.head_sha)
        ],
    }


def observation(snapshot: Snapshot) -> dict:
    """What one poll saw: the commit-pinned snapshot digest, plus the
    fingerprint that dedup keys on. Recorded every poll by the runner so a
    no-change poll is reconstructable and any decision is replayable from
    the log alone. Same whitelisted shape as event evidence."""
    return {"fingerprint": _fingerprint(snapshot), **_checks_evidence(snapshot)}


def _event(
    kind: str, task: Task, snapshot: Snapshot, now: float, message: str, rule: str
) -> WatchEvent:
    return WatchEvent(
        kind=kind,
        message=message,
        evidence={**_checks_evidence(snapshot), "rule": rule},
        terminal=kind in TERMINAL_KINDS,
        notable=kind in NOTABLE_KINDS,
        task_id=task.id,
        at=now,
    )


def _finish(state: dict, events: list[WatchEvent]) -> tuple[dict, list[WatchEvent]]:
    """Give each transition a stable identity, even if its evidence recurs.

    Replaying from the same saved state produces the same occurrence IDs;
    persisting state advances the sequence for a later failure/recovery cycle.
    """
    sequence = int(state.get("event_sequence", 0))
    identified = []
    for event in events:
        sequence += 1
        identified.append(replace(event, occurrence=str(sequence)))
    if events:
        state["event_sequence"] = sequence
    return state, identified


def step(task: Task, snapshot: Snapshot, now: float) -> tuple[dict, list[WatchEvent]]:
    """Advance the watch for one snapshot.

    Returns the new ``watch_state`` dict (persisted on the task) and the
    events to record and deliver. Dedup: an unchanged snapshot produces no
    events; terminal states are emitted exactly once.
    """
    task.validate()
    state: dict = dict(task.watch_state)
    events: list[WatchEvent] = []

    if state.get("terminal"):
        return state, []

    evidence = _checks_evidence(snapshot)

    # Terminal: the PR itself merged or closed — outranks check state.
    if snapshot.pr_state == "merged":
        events.append(
            _event(
                PR_MERGED, task, snapshot, now,
                f"{task.target} was merged", "stop: pr merged",
            )
        )
        state.update(terminal=True, terminal_kind=PR_MERGED, last_fingerprint=_fingerprint(snapshot))
        return _finish(state, events)
    if snapshot.pr_state == "closed":
        events.append(
            _event(
                PR_CLOSED, task, snapshot, now,
                f"{task.target} was closed", "stop: pr closed",
            )
        )
        state.update(terminal=True, terminal_kind=PR_CLOSED, last_fingerprint=_fingerprint(snapshot))
        return _finish(state, events)

    # A new head SHA resets evaluation — passing on the old commit never
    # carries over to the new one.
    last_sha = state.get("last_sha")
    if last_sha is not None and last_sha != snapshot.head_sha:
        state.pop("last_outcome", None)
        events.append(
            WatchEvent(
                kind=NEW_COMMIT,
                message=f"new commit {snapshot.head_sha[:10]} on {task.target}; "
                "previous results no longer apply",
                evidence={**evidence, "rule": "notify: new commit resets prior results"},
                notable=True,
                task_id=task.id,
                at=now,
            )
        )

    outcome = evaluate_checks(snapshot)
    state.update(last_sha=snapshot.head_sha)

    if outcome is CheckOutcome.PASSING:
        events.append(
            _event(
                CHECKS_PASSED,
                task,
                snapshot,
                now,
                f"required checks passed on {snapshot.head_sha[:10]} for {task.target}",
                "stop: required checks passed on the current head",
            )
        )
        state.update(terminal=True, terminal_kind=CHECKS_PASSED)
    elif outcome is CheckOutcome.FAILING:
        if state.get("last_outcome") != CheckOutcome.FAILING.value:
            failing = [
                run.name
                for run in failing_checks_on_current_commit(snapshot)
            ]
            events.append(
                _event(
                    CHECKS_FAILED,
                    task,
                    snapshot,
                    now,
                    f"checks failing on {snapshot.head_sha[:10]} for {task.target}: "
                    + ", ".join(failing),
                    "notify: checks failing on the current commit",
                )
            )
    else:  # PENDING or NO_CHECKS — intermediate polls, recorded not notified
        if snapshot.required_checks is None:
            pending_message = "required-check rules unavailable or unsupported; cannot confirm success"
        elif not snapshot.checks_complete:
            pending_message = "check results incomplete; cannot confirm success"
        elif not snapshot.required_checks:
            pending_message = "no required checks configured; watch remains active until PR closes or merges"
        else:
            pending_message = "required checks pending on the current commit"
        uncertain = (
            snapshot.required_checks is None
            or not snapshot.checks_complete
            or not snapshot.required_checks
        )
        reason_changed = state.get("last_pending_reason") != pending_message
        if reason_changed and (uncertain or state.get("last_outcome") is not None):
            events.append(
                WatchEvent(
                    kind=CHECKS_PENDING,
                    message=pending_message + f" ({task.target})",
                    evidence={
                        **evidence,
                        "rule": "record: success not confirmable",
                        "pending_reason": pending_message,
                    },
                    notable=False,
                    task_id=task.id,
                    at=now,
                )
            )
        state["last_pending_reason"] = pending_message

    if outcome not in (CheckOutcome.PENDING, CheckOutcome.NO_CHECKS):
        state.pop("last_pending_reason", None)

    fingerprint = _fingerprint(snapshot)
    if state.get("last_fingerprint") == fingerprint and not events:
        return state, []  # unchanged snapshot: nothing new
    state.update(
        last_fingerprint=fingerprint,
        last_outcome=outcome.value,
        last_event_at=now,
    )
    return _finish(state, events)

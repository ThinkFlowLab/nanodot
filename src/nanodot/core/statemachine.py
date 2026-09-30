"""The watch state machine — pure core, no I/O.

Consumes snapshots, decides transitions, emits events for the activity log
and notification sink. All of the issue-#1 safety rules live here:

- check results are commit-pinned (via core.github_eval);
- a new head SHA resets evaluation — old results never carry over;
- unchanged snapshots emit nothing (dedup);
- every terminal path emits exactly one terminal event.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from nanodot.core.github_eval import CheckOutcome, evaluate_checks
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


def _fingerprint(snapshot: Snapshot) -> str:
    checks = sorted(
        (run.name, run.status, run.conclusion or "") for run in snapshot.checks
    )
    payload = repr(
        (snapshot.head_sha, snapshot.pr_state, checks)
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _checks_evidence(snapshot: Snapshot) -> dict:
    return {
        "url": snapshot.url,
        "head_sha": snapshot.head_sha,
        "pr_state": snapshot.pr_state,
        "checks": [
            {"name": run.name, "status": run.status, "conclusion": run.conclusion}
            for run in snapshot.checks_for(snapshot.head_sha)
        ],
    }


def _event(
    kind: str, task: Task, snapshot: Snapshot, now: float, message: str
) -> WatchEvent:
    return WatchEvent(
        kind=kind,
        message=message,
        evidence=_checks_evidence(snapshot),
        terminal=kind in TERMINAL_KINDS,
        notable=kind in NOTABLE_KINDS,
        task_id=task.id,
        at=now,
    )


def step(task: Task, snapshot: Snapshot, now: float) -> tuple[dict, list[WatchEvent]]:
    """Advance the watch for one snapshot.

    Returns the new ``watch_state`` dict (persisted on the task) and the
    events to record and deliver. Dedup: an unchanged snapshot produces no
    events; terminal states are emitted exactly once.
    """
    state: dict = dict(task.watch_state)
    events: list[WatchEvent] = []

    if state.get("terminal"):
        return state, []

    evidence = _checks_evidence(snapshot)

    # Terminal: the PR itself merged or closed — outranks check state.
    if snapshot.pr_state == "merged":
        events.append(
            _event(PR_MERGED, task, snapshot, now, f"{task.target} was merged")
        )
        state.update(terminal=True, terminal_kind=PR_MERGED, last_fingerprint=_fingerprint(snapshot))
        return state, events
    if snapshot.pr_state == "closed":
        events.append(
            _event(PR_CLOSED, task, snapshot, now, f"{task.target} was closed")
        )
        state.update(terminal=True, terminal_kind=PR_CLOSED, last_fingerprint=_fingerprint(snapshot))
        return state, events

    # A new head SHA resets evaluation — passing on the old commit never
    # carries over to the new one.
    last_sha = state.get("last_sha")
    if last_sha is not None and last_sha != snapshot.head_sha:
        events.append(
            WatchEvent(
                kind=NEW_COMMIT,
                message=f"new commit {snapshot.head_sha[:10]} on {task.target}; "
                "previous results no longer apply",
                evidence=evidence,
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
                f"all checks passed on {snapshot.head_sha[:10]} for {task.target}",
            )
        )
        state.update(terminal=True, terminal_kind=CHECKS_PASSED)
    elif outcome is CheckOutcome.FAILING:
        if state.get("last_outcome") != CheckOutcome.FAILING.value:
            failing = [
                run.name
                for run in snapshot.checks_for(snapshot.head_sha)
                if run.conclusion in ("failure", "timed_out", "cancelled", "action_required")
            ]
            events.append(
                _event(
                    CHECKS_FAILED,
                    task,
                    snapshot,
                    now,
                    f"checks failing on {snapshot.head_sha[:10]} for {task.target}: "
                    + ", ".join(failing),
                )
            )
    else:  # PENDING or NO_CHECKS — intermediate polls, recorded not notified
        if state.get("last_outcome") != outcome.value and state.get("last_outcome") is not None:
            events.append(
                WatchEvent(
                    kind=CHECKS_PENDING,
                    message=(
                        "checks pending on the current commit"
                        if outcome is CheckOutcome.PENDING
                        else "no check runs on the current commit yet"
                    )
                    + f" ({task.target})",
                    evidence=evidence,
                    notable=False,
                    task_id=task.id,
                    at=now,
                )
            )

    fingerprint = _fingerprint(snapshot)
    if state.get("last_fingerprint") == fingerprint and not events:
        return state, []  # unchanged snapshot: nothing new
    state.update(
        last_fingerprint=fingerprint,
        last_outcome=outcome.value,
        last_event_at=now,
    )
    return state, events

"""Pure evaluation of a snapshot's check state — commit-pinned by rule.

Only check runs whose SHA equals the snapshot's current head SHA count.
Check runs from older commits are data, never satisfaction.
"""

from __future__ import annotations

from enum import Enum

from nanodot.ports.github import Snapshot

PASSING_CONCLUSIONS = frozenset({"success"})
FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required"})
IGNORED_CONCLUSIONS = frozenset({"skipped", "neutral"})


class CheckOutcome(str, Enum):
    PASSING = "passing"
    FAILING = "failing"
    PENDING = "pending"  # checks exist but not all completed
    NO_CHECKS = "no_checks"  # no runs on the current head SHA


def evaluate_checks(snapshot: Snapshot) -> CheckOutcome:
    """Required-checks outcome for the snapshot's current head SHA only."""
    runs = snapshot.checks_for(snapshot.head_sha)
    if not runs:
        return CheckOutcome.NO_CHECKS
    if any(run.status != "completed" for run in runs):
        return CheckOutcome.PENDING
    for run in runs:
        if run.conclusion in FAILING_CONCLUSIONS:
            return CheckOutcome.FAILING
    if all(run.conclusion in PASSING_CONCLUSIONS for run in runs):
        return CheckOutcome.PASSING
    # Completed runs that are neither failing nor all-success (e.g. all
    # skipped) — not a confirmed terminal pass.
    return CheckOutcome.PENDING


def checks_passing_on_current_commit(snapshot: Snapshot) -> bool:
    """The watch satisfaction rule: every check on the *current* head SHA
    completed with success."""
    return evaluate_checks(snapshot) is CheckOutcome.PASSING

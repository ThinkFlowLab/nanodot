"""The task loop — one bounded iteration of fetch → decide → record.

Core logic only: the loop depends on the SnapshotFetcher and
NotificationSink ports, never on their native implementations. The
scheduler/executor port (see adapter-seam.md) drives `run_once`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from enum import Enum

from nanodot.core import statemachine
from nanodot.core.activity import ActivityLog
from nanodot.core.memory import MemoryStore
from nanodot.core.statemachine import WatchEvent
from nanodot.core.tasks import Task, TaskError, TaskState, TaskStore
from nanodot.core.write_flow import WriteFlow
from nanodot.ports.github import (
    AuthLostError,
    FetchError,
    PRNotFoundError,
    RetryableError,
    SnapshotFetcher,
)
from nanodot.ports.inference import InferenceProvider, StateChange
from nanodot.ports.notifier import NotificationSink

MAX_BACKOFF_SECONDS = 3600
PROLONGED_FAILURE_SECONDS = 3600
SUMMARY_BUDGET_SECONDS = 1.0
CHECK_OBSERVED = "check-observed"  # per-poll record of what the runner saw
OBSERVATION_RETENTION = 20  # recent per-poll digests kept per task


def state_change_from_event(event: WatchEvent) -> StateChange:
    """The whitelisted evidence for a summary, from an event."""
    checks = tuple(
        (check.get("name", ""), check.get("conclusion"))
        for check in event.evidence.get("checks", [])
    )
    return StateChange(
        kind=event.kind,
        summary=event.message,
        head_sha=event.evidence.get("head_sha", ""),
        pr_state=event.evidence.get("pr_state", ""),
        url=event.evidence.get("url", ""),
        checks=checks,
    )


def safe_summarize(provider: InferenceProvider, event: WatchEvent) -> str | None:
    """Summarize through the provider; degrade to None on any failure.
    The summary decorates the notification — the raw message is the truth."""
    try:
        return provider.summarize(state_change_from_event(event))
    except Exception:
        # Optional decoration must never interrupt core checking, even for
        # a malformed third-party provider implementation.
        return None


class _SummaryBudget:
    """At most one optional call in flight; overdue summaries are discarded.

    A daemon thread bounds scheduler wait even for a misbehaving adapter.
    Python cannot forcibly cancel arbitrary calls, so subsequent summaries
    are skipped until that call finishes rather than spawning more threads.
    """

    def __init__(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("summary budget must not be negative")
        self._seconds = seconds
        self._inflight = threading.Lock()

    def summarize(self, provider: InferenceProvider, event: WatchEvent) -> str | None:
        if not self._inflight.acquire(blocking=False):
            return None
        completed = threading.Event()
        result: list[str | None] = []

        def work() -> None:
            try:
                result.append(safe_summarize(provider, event))
            finally:
                self._inflight.release()
                completed.set()

        worker = threading.Thread(target=work, name="nanodot-summary", daemon=True)
        try:
            worker.start()
        except Exception:
            self._inflight.release()
            return None
        if not completed.wait(self._seconds):
            return None
        return result[0] if result else None

    def drain(self, seconds: float) -> None:
        """Wait, bounded by ``seconds``, for an in-flight summary to finish.

        A provider that will not finish costs the wait, not the shutdown:
        the drain gives up and the summary is abandoned mid-flight.
        """
        if self._inflight.acquire(timeout=max(0.0, seconds)):
            self._inflight.release()


class RunOutcome(str, Enum):
    OK = "ok"
    TERMINAL = "terminal"
    RETRY_SCHEDULED = "retry-scheduled"
    BLOCKED = "blocked"
    SKIPPED_TERMINAL = "skipped-terminal"
    SKIPPED_OVERLAP = "skipped-overlap"
    SKIPPED_INACTIVE = "skipped-inactive"
    SKIPPED_SCOPE_CHANGED = "skipped-scope-changed"


def backoff_seconds(cadence_seconds: int, consecutive_failures: int) -> int:
    """Exponential backoff x2 per failure, capped at one hour."""
    return min(cadence_seconds * (2 ** consecutive_failures), MAX_BACKOFF_SECONDS)


class TaskLoop:
    def __init__(
        self,
        store: TaskStore,
        fetcher: SnapshotFetcher,
        sink: NotificationSink,
        activity: ActivityLog,
        provider: InferenceProvider | None = None,
        memory: MemoryStore | None = None,
        summary_budget_seconds: float = SUMMARY_BUDGET_SECONDS,
        write: WriteFlow | None = None,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._sink = sink
        self._activity = activity
        self._provider = provider
        self._memory = memory
        self._write = write
        self._summary_budget_seconds = summary_budget_seconds
        self._summaries = _SummaryBudget(summary_budget_seconds)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(task_id, threading.Lock())

    def close(self) -> None:
        """Release the loop's own resources: drain an in-flight summary
        (bounded by the summary budget) and drop the per-task locks.

        Call after serve/tick has returned. Safe to call more than once;
        a later run_once rebuilds whatever it needs.
        """
        self._summaries.drain(self._summary_budget_seconds)
        with self._locks_guard:
            self._locks.clear()

    def run_once(self, task: Task, now: float) -> RunOutcome:
        """One bounded check for one task. Safe to call concurrently:
        a second run of the same task is skipped, never overlapped."""
        lock = self._lock_for(task.id)
        if not lock.acquire(blocking=False):
            return RunOutcome.SKIPPED_OVERLAP
        try:
            # Reconcile stale executor inputs with the latest saved lifecycle.
            saved = self._store.get(task.id)
            if saved is not None:
                if saved.state.terminal or saved.watch_state.get("terminal"):
                    return RunOutcome.SKIPPED_TERMINAL
                if saved.state is not TaskState.ACTIVE:
                    return RunOutcome.SKIPPED_INACTIVE
            if task.state.terminal or task.watch_state.get("terminal"):
                return RunOutcome.SKIPPED_TERMINAL
            if task.state is not TaskState.ACTIVE:
                return RunOutcome.SKIPPED_INACTIVE
            # Mutable tasks and alternate executors must not bypass scope
            # validation. Invalid legacy rows remain inspectable/cancellable.
            try:
                self._store.validate(task)
                if saved is not None:
                    self._store.validate(saved)
            except TaskError as error:
                self._store.set_blocked(task.id, f"invalid saved task scope: {error}")
                return RunOutcome.BLOCKED
            if saved is not None:
                task = saved
            return self._run_locked(task, now)
        finally:
            lock.release()

    # -- internals ---------------------------------------------------------

    def _superseded(self, task: Task) -> RunOutcome | None:
        """A pause/cancel/scope edit during a fetch takes effect before delivery."""
        saved = self._store.get(task.id)
        if saved is None:
            raise TaskError(f"no such task {task.id}")
        if saved.state.terminal or saved.watch_state.get("terminal"):
            return RunOutcome.SKIPPED_TERMINAL
        if saved.state is not TaskState.ACTIVE:
            return RunOutcome.SKIPPED_INACTIVE
        if saved.scope_version != task.scope_version:
            return RunOutcome.SKIPPED_SCOPE_CHANGED
        return None

    def _record(
        self, task_id: str, kind: str, message: str,
        evidence: dict | None, at: float,
    ) -> None:
        """Append to the activity log without failing the run: task state
        and delivery never depend on the log accepting an entry."""
        try:
            self._activity.append(
                task_id=task_id, kind=kind, message=message,
                evidence=evidence, at=at,
            )
        except Exception:
            # Do not log exception text; it may include private contents.
            logging.getLogger(__name__).warning(
                "task %s: %s entry could not be saved to the activity log",
                task_id, kind,
            )

    def _record_event(self, event: WatchEvent) -> None:
        """Record an event with its replay identity: the occurrence the
        notification sink already deduplicates on, so a crash-and-replay
        duplicate is detectable from the log alone."""
        evidence = dict(event.evidence or {})
        if event.occurrence is not None:
            evidence.setdefault("occurrence", event.occurrence)
        self._record(
            event.task_id, event.kind, event.message, evidence, event.at
        )

    def _run_locked(self, task: Task, now: float) -> RunOutcome:
        # Writes precede fetching: an approved capability executes even if
        # this poll later fails, and recovery surfaces crash doubts first.
        if self._write is not None:
            for event in self._write.recover(task, now):
                self._sink.notify(event)
            for event in self._write.execute_pending(
                task, now, superseded=lambda: bool(self._superseded(task))
            ):
                self._sink.notify(event)

        try:
            snapshot = self._fetcher.fetch(task.target)
        except (AuthLostError, PRNotFoundError) as error:
            if skipped := self._superseded(task):
                return skipped
            self._block(task, error, now)
            return RunOutcome.BLOCKED
        except RetryableError as error:
            if skipped := self._superseded(task):
                return skipped
            return self._schedule_retry(task, error, now)
        except FetchError as error:  # unexpected — treat as retryable
            if skipped := self._superseded(task):
                return skipped
            return self._schedule_retry(task, error, now)

        if skipped := self._superseded(task):
            return skipped
        failures = int(task.watch_state.get("consecutive_failures", 0))
        task.watch_state = dict(
            task.watch_state, consecutive_failures=0, last_success_at=now
        )
        if failures and task.blocker:
            task.blocker = None  # keep the in-memory task consistent too
            self._store.clear_flag(task.id)

        watch_state, events = statemachine.step(task, snapshot, now)
        task.watch_state = watch_state

        # A pause/cancel/scope edit during fetching takes effect before
        # anything is recorded or delivered: a superseded run leaves no
        # activity entries and sends nothing to a provider.
        if skipped := self._superseded(task):
            return skipped

        # Observe → decide → act: every poll records what it saw before the
        # events it produced, so the log alone replays every decision. The
        # digest is high-volume by design; retention bounds it to the most
        # recent OBSERVATION_RETENTION rows per task.
        self._record(
            task.id,
            CHECK_OBSERVED,
            f"observed {snapshot.head_sha[:10]} ({snapshot.pr_state})",
            statemachine.observation(snapshot),
            now,
        )
        self._prune_observations(task.id)

        for event in events:
            # Record before any optional egress: nothing may reach a provider
            # that the activity log cannot already reconstruct (adapter-seam
            # discipline 3), and delivery can still be suppressed for a run
            # superseded while the summary was in flight.
            self._record_event(event)
            if event.notable:
                summary = None
                if self._provider is not None:
                    summary = self._summaries.summarize(self._provider, event)
                    if skipped := self._superseded(task):
                        return skipped
                if summary:
                    event = replace(event, summary=summary)
                self._sink.notify(event)

        # In gated mode a failure event proposes one comment for human
        # approval — content-bound, never sent without it.
        if self._write is not None:
            for event in events:
                proposed = self._write.propose_write(task, event)
                if proposed is not None:
                    self._sink.notify(proposed)

        terminal = any(event.terminal for event in events)
        if terminal:
            terminal_event = next(e for e in events if e.terminal)
            # Persist completion before optional memory work. A failed
            # observation cannot leave an ACTIVE task with terminal state.
            task.state = TaskState.COMPLETED
            task.next_check_at = None
            self._store.update(task)
            if self._memory is not None:
                # Write path 2: evidenced terminal outcome, auto-recorded
                # as an observation with provenance to the evidence.
                try:
                    self._memory.add_observation(
                        content=f"{task.target}: {terminal_event.message}",
                        task_id=task.id,
                        evidence_ref=(
                            f"{terminal_event.evidence.get('url', '')}@"
                            f"{terminal_event.evidence.get('head_sha', '')}"
                        ),
                        at=now,
                    )
                except Exception:
                    # Do not log provider/store exception text; it may include
                    # private contents. The durable terminal result is intact.
                    logging.getLogger(__name__).warning(
                        "task %s completed, but its observation could not be saved",
                        task.id,
                    )
            return RunOutcome.TERMINAL

        task.next_check_at = now + task.cadence_seconds
        self._store.update(task)
        return RunOutcome.OK

    def _prune_observations(self, task_id: str) -> None:
        """Retention is best-effort: a failing prune must not fail the run."""
        try:
            self._activity.prune_observations(task_id, OBSERVATION_RETENTION, kind=CHECK_OBSERVED)
        except Exception:
            logging.getLogger(__name__).warning(
                "task %s: observation history could not be pruned", task_id
            )

    def _block(self, task: Task, error: Exception, now: float) -> None:
        reason = f"blocked: {error}"
        sequence = int(task.watch_state.get("event_sequence", 0)) + 1
        event = WatchEvent(
            kind=statemachine.BLOCKED,
            message=reason,
            evidence={},
            notable=True,
            task_id=task.id,
            at=now,
            occurrence=str(sequence),
        )
        # The log is isolation-guarded like every other write: a failing
        # append must still notify the user and persist the BLOCKED state.
        self._record_event(event)
        self._sink.notify(event)
        task.watch_state = dict(task.watch_state, event_sequence=sequence)
        task.state = TaskState.BLOCKED
        task.blocker = reason
        task.next_check_at = None
        self._store.update(task)

    def _schedule_retry(self, task: Task, error: Exception, now: float) -> RunOutcome:
        failures = int(task.watch_state.get("consecutive_failures", 0)) + 1
        watch_state = dict(task.watch_state)
        watch_state["consecutive_failures"] = failures
        if "last_success_at" not in watch_state:
            watch_state["last_success_at"] = now  # first run: start the clock
        task.watch_state = watch_state

        task.next_check_at = now + backoff_seconds(task.cadence_seconds, failures)
        self._store.update(task)

        # Isolation-guarded like every activity write: the retry is already
        # durably scheduled, a failing append must not double-count it.
        self._record(
            task_id=task.id,
            kind="fetch-retry",
            message=f"retryable fetch failure ({failures}): {error}",
            evidence=None,
            at=now,
        )

        without_success = now - float(watch_state["last_success_at"])
        if without_success >= PROLONGED_FAILURE_SECONDS and not task.blocker:
            self._store.flag(
                task.id,
                f"prolonged fetch failure: no successful check for "
                f"{int(without_success)}s ({failures} attempts)",
            )
        return RunOutcome.RETRY_SCHEDULED

"""The write flow — propose, approve, execute, recover (docs/design/github-writer.md).

Core logic only: drafting is a deterministic function of event evidence
(no model), execution is intent-first (the durable intent precedes the
non-idempotent POST), and recovery admits uncertainty (an intent without
an outcome is surfaced, its capability burned, never blindly re-sent).
"""

from __future__ import annotations

import logging
from typing import Callable

from nanodot.core.activity import ActivityLog
from nanodot.core.permissions import (
    WRITE_REQUEST_TTL_SECONDS,
    Mode,
    PermissionCenter,
)
from nanodot.core.redaction import Redactor
from nanodot.core.statemachine import CHECKS_FAILED, WatchEvent
from nanodot.core.tasks import Task
from nanodot.ports.github_writer import (
    GitHubWriter,
    WriteCapability,
    WriteError,
    payload_digest,
)

COMMENT_ACTION = "comment"
# The one implemented write action, scoped to the watch's own target.
WATCH_SCOPE = "watch"

# Activity kinds — the write trail is replayable from the log alone.
WRITE_PROPOSED = "write-proposed"
WRITE_APPROVED = "write-approved"  # appended by the CLI, not the runner
WRITE_AUTO = "write-auto"  # auto mode: a standing grant authorized this payload
WRITE_SKIPPED = "write-skipped"  # auto mode: no standing grant; nothing sent or asked
WRITE_SILENCE_DENIED = "write-silence-denied"  # two expired asks; terminal (#48 decision 3)
WRITE_INTENT = "write-intent"
WRITE_DONE = "write-done"
WRITE_FAILED = "write-failed"
WRITE_UNKNOWN = "write-unknown"

_RECOVERY_SCAN = (WRITE_INTENT, WRITE_DONE, WRITE_FAILED, WRITE_UNKNOWN)
# A digest already seen in any of these is never re-authorized: auto writes
# once per exact content, exactly like the gated re-ask rules.
_AUTO_SEEN_SCAN = (WRITE_AUTO, WRITE_INTENT, WRITE_DONE, WRITE_FAILED, WRITE_SKIPPED)


def draft_failure_comment(task: Task, event: WatchEvent) -> dict:
    """Deterministic, rule-drafted payload from evidence already in hand.
    Same evidence in, same canonical bytes out — the approval hash and the
    wire body share this dict (payload_digest)."""
    sha = str(event.evidence.get("head_sha", ""))[:10] or "unknown head"
    failing = [
        str(check.get("name", ""))
        for check in event.evidence.get("checks", [])
        if check.get("conclusion") == "failure"
    ]
    names = ", ".join(name for name in failing if name) or "unspecified checks"
    body = (
        f"[nanodot automated watch] Required checks failing on {sha} "
        f"for {task.target}: {names}. Watching continues until they pass "
        f"or the PR merges or closes."
    )
    return {"body": body}


class WriteFlow:
    def __init__(
        self,
        permissions: PermissionCenter,
        writer: GitHubWriter,
        activity: ActivityLog,
        redactor: Redactor | None = None,
    ) -> None:
        self._permissions = permissions
        self._writer = writer
        self._activity = activity
        # The requests table is a persistence boundary: content is scrubbed
        # before it is hashed and stored, so what was approved, what is
        # logged, and what is sent are the same secret-free bytes.
        self._redactor = redactor or Redactor(_NullSecrets())

    # -- proposing -------------------------------------------------------------

    def propose_write(self, task: Task, event: WatchEvent) -> WatchEvent | None:
        """On a checks-failed event, draft the comment and route by mode.
        Gated: request approval (a pending or denied request for this exact
        content is never duplicated or re-asked). Auto: a standing grant
        derives a single-use content-bound capability with no prompt —
        grant-less content is skipped and recorded once. Readonly: nothing.
        Returns the notification event, or None."""
        if event.kind != CHECKS_FAILED:
            return None
        mode = self._permissions.mode()
        if mode is Mode.READONLY:
            return None
        try:
            self._permissions.assert_allowed(COMMENT_ACTION)
        except Exception:
            return None
        payload = draft_failure_comment(task, event)
        payload["body"] = self._redactor.scrub(payload["body"])
        digest = payload_digest(payload)
        scope_hint = f"{WATCH_SCOPE}:{event.evidence.get('head_sha', '')[:10]}"
        if mode is Mode.GATED:
            states = self._permissions.verbatim_request_states(task.id, digest)
            state_names = [state for state, _ in states]
            if "pending" in state_names or "denied" in state_names:
                return None  # never duplicate a pending ask, never re-ask a denial
            expired = state_names.count("expired")
            if expired >= 2:
                # The second silence is an answer (#48 decision 3): the
                # latest expired ask becomes a terminal denial.
                self._permissions.mark_silence_denied(states[0][1])
                denied = WatchEvent(
                    kind=WRITE_SILENCE_DENIED,
                    message=(
                        f"comment on {task.target} asked twice with no answer "
                        "— recorded as denied by silence and never re-asked"
                    ),
                    evidence={
                        "action": COMMENT_ACTION,
                        "target": str(task.target),
                        "content_hash": digest,
                    },
                    notable=True,
                    task_id=task.id,
                    at=event.at,
                    occurrence=digest,
                )
                self._append(denied)
                return denied
            request = self._permissions.request(
                action=COMMENT_ACTION,
                target=str(task.target),
                scope=scope_hint,
                task_id=task.id,
                content=payload,
                ttl=WRITE_REQUEST_TTL_SECONDS,
            )
            proposed = WatchEvent(
                kind=WRITE_PROPOSED,
                message=(
                    f"comment proposed on {task.target} — approve with: "
                    f"nanodot approvals approve {request.id}"
                ),
                evidence={
                    "request_id": request.id,
                    "action": COMMENT_ACTION,
                    "target": str(task.target),
                    "content_hash": digest,
                    "content": payload["body"],
                },
                notable=True,
                task_id=task.id,
                at=event.at,
                occurrence=request.id,
            )
            self._append(proposed)
            return proposed
        # AUTO — the standing-grant scope is the stable watch scope, so one
        # pre-grant covers the watch; the payload hash is bound at issue time.
        if self._auto_seen(task.id, digest):
            return None
        capability = self._permissions.authorize_auto(
            action=COMMENT_ACTION,
            target=str(task.target),
            scope=WATCH_SCOPE,
            task_id=task.id,
            payload=payload,
        )
        if capability is None:
            return self._record_skip(task, event, digest)
        authorized = WatchEvent(
            kind=WRITE_AUTO,
            message=(
                f"comment auto-authorized on {task.target} by a standing grant "
                "— it will be sent on this run"
            ),
            evidence={
                "grant_id": capability.grant_id,
                "action": COMMENT_ACTION,
                "target": str(task.target),
                "content_hash": digest,
                "content": payload["body"],
            },
            notable=True,
            task_id=task.id,
            at=event.at,
            occurrence=capability.grant_id,
        )
        self._append(authorized)
        return authorized

    def _auto_seen(self, task_id: str, digest: str) -> bool:
        """This exact content was already authorized, sent, failed, or
        skipped: auto never repeats it. A new head SHA drafts new content."""
        entries = self._activity.query(
            task_id=task_id, kinds=_AUTO_SEEN_SCAN, limit=500
        )
        return any(
            (entry.evidence or {}).get("content_hash") == digest for entry in entries
        )

    def _record_skip(
        self, task: Task, event: WatchEvent, digest: str
    ) -> WatchEvent | None:
        """Grant-less auto content: recorded once (never spammed per poll),
        never prompted, nothing sent. A standing grant added later applies
        only to content not yet seen."""
        skipped = WatchEvent(
            kind=WRITE_SKIPPED,
            message=(
                f"comment on {task.target} skipped: no standing grant "
                f"for {COMMENT_ACTION} — pre-grant with "
                f"nanodot approvals grant (issue #55)"
            ),
            evidence={
                "action": COMMENT_ACTION,
                "target": str(task.target),
                "content_hash": digest,
            },
            notable=True,
            task_id=task.id,
            at=event.at,
            occurrence=digest,
        )
        self._append(skipped)
        return skipped

    # -- executing ---------------------------------------------------------------

    def execute_pending(
        self, task: Task, now: float,
        superseded: Callable[[], bool] | None = None,
    ) -> list[WatchEvent]:
        """Execute every approved-but-unconsumed capability for this task:
        durable intent → single-use consume → send → outcome. Any doubt
        fails closed with nothing sent."""
        events: list[WatchEvent] = []
        for capability, payload in self._permissions.pending_capabilities(task.id):
            if superseded is not None and superseded():
                return events
            if payload_digest(payload) != capability.content_hash:
                # The stored payload no longer matches what was approved.
                events.append(self._finish(task, capability, "content hash mismatch", now))
                continue
            # The single-use consume is the forgery gate: only a capability
            # the store actually issued (and no one has used) is sendable.
            if not self._permissions.consume(capability.grant_id):
                continue  # forged, lost the race, or replayed: never send
            if not self._log_intent(task, capability, now):
                continue  # no durable intent, no send — fail closed
            if superseded is not None and superseded():
                return events  # consumed but unsent: recovery owns the doubt
            events.append(self._finish(task, capability, self._send(capability, payload), now))
        return events

    def _send(self, capability: WriteCapability, payload: dict) -> str:
        """Returns an error string; an empty string means the write left."""
        try:
            self._writer.execute(capability, payload)
            return ""
        except WriteError as error:
            return type(error).__name__
        except Exception:
            # A misbehaving writer is an outcome, never a crash of the run.
            return "writer failed"

    def _log_intent(self, task: Task, capability: WriteCapability, now: float) -> bool:
        """The intent must be durable before the POST: a failed append
        aborts the send (fail closed), unlike every other log write."""
        try:
            self._activity.append(
                task_id=task.id,
                kind=WRITE_INTENT,
                message=f"write intent: {capability.action} on {capability.target}",
                evidence={
                    "grant_id": capability.grant_id,
                    "action": capability.action,
                    "target": capability.target,
                    "content_hash": capability.content_hash,
                },
                at=now,
            )
            return True
        except Exception:
            logging.getLogger(__name__).warning(
                "task %s: write intent could not be saved; nothing was sent",
                task.id,
            )
            return False

    def _finish(
        self, task: Task, capability: WriteCapability,
        error: str, now: float,
    ) -> WatchEvent:
        if not error:
            event = WatchEvent(
                kind=WRITE_DONE,
                message=f"comment posted on {capability.target}",
                # url: the result body is provider-shaped; the trail is the log
                evidence={
                    "grant_id": capability.grant_id,
                    "content_hash": capability.content_hash,
                },
                notable=True,
                task_id=task.id,
                at=now,
                occurrence=capability.grant_id,
            )
        else:
            event = WatchEvent(
                kind=WRITE_FAILED,
                message=f"comment on {capability.target} failed: {error}",
                evidence={
                    "grant_id": capability.grant_id,
                    "content_hash": capability.content_hash,
                },
                notable=True,
                task_id=task.id,
                at=now,
                occurrence=capability.grant_id,
            )
        self._append(event)
        return event

    # -- recovery -----------------------------------------------------------------

    def recover(self, task: Task, now: float) -> list[WatchEvent]:
        """After a restart, an intent without an outcome is UNKNOWN: surface
        it once in the inbox and burn its capability so it can never be
        sent later. Never re-send blindly."""
        entries = self._activity.query(task_id=task.id, kinds=_RECOVERY_SCAN, limit=500)
        intents: dict[str, str] = {}
        resolved: set[str] = set()
        for entry in entries:  # newest first; replay order restored below
            grant_id = entry.evidence.get("grant_id")
            if not grant_id:
                continue
            if entry.kind == WRITE_INTENT:
                intents.setdefault(grant_id, entry.id)
            else:
                resolved.add(grant_id)
        events: list[WatchEvent] = []
        for grant_id in intents.keys() - resolved:
            self._permissions.consume(grant_id)  # burn: never send the unknown
            event = WatchEvent(
                kind=WRITE_UNKNOWN,
                message=(
                    f"a comment write on {task.target} has an unknown outcome "
                    f"(crash or restart mid-write); it will NOT be re-sent — "
                    "please check the PR and, if absent, approve a new proposal"
                ),
                evidence={"grant_id": grant_id},
                notable=True,
                task_id=task.id,
                at=now,
                occurrence=grant_id,
            )
            self._append(event)
            events.append(event)
        return list(reversed(events))

    # -- internals -------------------------------------------------------------

    def _append(self, event: WatchEvent) -> None:
        """Isolation-guarded like every activity write: the run and delivery
        never depend on the log accepting an entry (intents are the
        exception — see _log_intent)."""
        try:
            self._activity.append(
                task_id=event.task_id, kind=event.kind, message=event.message,
                evidence=dict(event.evidence or {}), at=event.at,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "task %s: %s entry could not be saved to the activity log",
                event.task_id, event.kind,
            )


class _NullSecrets:
    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None: ...

    def unset(self, name: str) -> None: ...

    def names(self) -> list[str]:
        return []

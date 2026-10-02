# The activity log is a decision log

Status: decided 2026-10-02 (issue #35) · implemented MVP

The activity log records what the runner observed, not just what changed.

- Every completed poll appends a `check-observed` entry before any events it
  produced: the commit-pinned snapshot digest plus the dedup fingerprint
  (`statemachine.observation`). A no-change poll is reconstructable.
- Every event's evidence names the policy rule that fired — `stop: …`,
  `notify: …`, or `record: …`, plus for pending states the concrete
  `pending_reason` (`statemachine.step`).
- Entries written at the same second replay in insertion order
  (observe → decide → act).

Decisions are deterministic given `(task, snapshot, clock)`, so the log alone
replays every stop/notify decision without re-asking GitHub — auditability as
the runtime extension of "substitution is the proof" (adapter-seam.md).

## Guarantees and boundaries

- A run overtaken by pause/cancel/scope change records nothing: the
  supersession check runs after summarizing and before any entry is written
  or any event is delivered, and recording is all-or-nothing per run.
- An activity-write failure never fails the run or blocks delivery; task
  state and notifications do not depend on the log accepting an entry.
- No new egress: the digest is the same whitelisted shape as event evidence
  (egress.md), redacted at write time like every other entry.
- No schema migration: `activity.evidence` is free-form JSON.

## CLI

`nanodot activity` hides `check-observed` by default so events stay readable;
`--all` includes them. The `watch list` and `watch show` summaries always skip
observations.

The replay-complete record follows the pi agent harness's append-only session
transcripts ("the transcript is the trust artifact"), keeping our own trust
model: observations stay local, whitelisted-shaped, and consumed by code
(the state machine), not by a human in the loop.

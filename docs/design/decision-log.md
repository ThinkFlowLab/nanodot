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

Write decisions join the same guarantee (issues #54–#56): every write is a
replayable trail — `write-proposed` (the ask, with content hash and body),
`write-approved` (the CLI's approval), `write-intent` (durable before the
POST), then `write-done` / `write-failed` / `write-unknown`; budget
exhaustion records `quota-exhausted` once per action/watch/day, and a
second unanswered ask records `write-silence-denied`. The bytes at the HTTP
boundary hash-equal the approved content hash by construction, so the log
plus the permission tables reconstruct every write decision offline:
what was asked, who approved it, what was sent, and what refused to leave.

## Guarantees and boundaries

- A run overtaken by pause/cancel/scope change records nothing that the
  runner did not already do: the supersession check before recording stops
  observation and delivery entirely, and a change landing while a summary is
  in flight suppresses delivery — but the already-sent summary means the log
  keeps what the provider saw (adapter-seam discipline 3).
- An activity-write failure never fails the run or blocks delivery; task
  state and notifications do not depend on the log accepting an entry — on
  every write path, including blocked and retry outcomes.
- Retention bounds the per-poll history: each task keeps its most recent
  `runner.OBSERVATION_RETENTION` `check-observed` entries (pruned after each
  record); decisions and delivery records are never pruned. Event entries
  carry the same `occurrence` identity the notification sink deduplicates
  on, so a crash-and-replay duplicate is detectable from the log alone.
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

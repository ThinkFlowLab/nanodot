# The GitHub writer port — bounded, approved writes

Status: decided 2026-10-03 (A/B analysis) · GATED implemented (#59/#62);
AUTO activated as the default mode per #48 decision 5 (standing grants)

Enables the always-on goal: the agent may *propose* external writes, but a
write executes only behind a human-approved, content-bound, single-use
capability — by construction, not convention.

## Decision A: port shape

1. **A separate port.** `SnapshotFetcher` stays read-only in every
   implementation (adapter-seam rule, egress §1, the #38 tripwire are not
   amended). Writes live behind a new `ports/github_writer.py` Protocol;
   `native` implements it; tests fake it. Readers never see write
   semantics.
2. **Approvals bind content.** `requests`/`grants` gain a `content_hash`
   column (SHA-256 of the exact payload; additive migration). `permits()`
   still matches action+target+scope+task; *execution* additionally
   requires hash equality with the approved payload. Approved-then-mutated
   content fails closed (TOCTOU).
3. **Capability, single-use.** `PermissionCenter.approve()` issues a
   one-shot `WriteCapability(action, target, content_hash, grant_id)` —
   the frozen port dataclass from #61; single-use is enforced by an
   atomic `UPDATE … WHERE used_at IS NULL`, which doubles as the forgery
   gate: a capability the store never issued cannot be consumed and so
   is never sent. The writer port's only public method is
   `execute(capability, payload)` — canonical-JSON payload, hash verified
   at the wire. Core cannot call the writer without a capability; a
   capability cannot execute without the store.
4. **Self-approval is structurally absent.** `approve()` is called only by
   the CLI; no runner/loop/core path approves anything, ever. (Structural
   test in the acceptance list.)
5. **Crash protocol — intent first.** POSTs are not idempotent:
   the decision log records `write-intent` (capability id, endpoint,
   content hash) **before** the request, `write-done`/`write-failed`
   after. On restart an intent without an outcome is UNKNOWN: never
   re-sent blindly; surfaced in the inbox; optionally reconciled via the
   read port (author + time-window + content-prefix search). This mirrors
   commit-pinning: uncertainty is admitted, never papered over.
6. **First action: a comment on a watched PR**, rule-drafted from evidence
   already in hand (no model coupling — model drafting is a separate,
   explicit egress decision). Merge and release are out of scope;
   class-level standing grants remain the dormant AUTO mode.
7. **Existing machinery applies unchanged.** Grants die on scope change
   (`on_scope_change`), executions pass the runner's supersession checks,
   `Mode.READONLY` still raises on every write. All three modes are live:
   GATED is the interactive loop, AUTO is the default (#48 decision 5),
   below.

## Decision B: identity

- **Now: a fine-grained PAT in a separate secret-store key**
  (`github-write-token`) — per-repository, comment-write only, short
  expiry, created by the user on github.com. nanodot still creates no
  tokens and joins no authorization flows. The read token never reaches
  the writer; the write token never reaches the fetcher (blast-radius
  split, asserted on fake-transport Authorization headers).
- **GitHub App: deferred, not blocked.** An app needs owner installation
  (unavailable on third-party repos — the primary watch case), adds a
  long-lived private key, and pulls webhooks against local-first. If ever
  adopted it slots in as another native writer implementation behind the
  same port; core does not change. B is an implementation detail precisely
  because A's port absorbs it.

## AUTO: standing grants (default mode, #48 decision 5)

AUTO never prompts. A **standing grant** — a grant with an empty
`content_hash`, created only by an explicit CLI act (`approvals grant`,
#55) — pre-authorizes an action+target+scope without binding payload
bytes. At run time `PermissionCenter.authorize_auto` matches a live
standing grant and derives a **single-use, content-bound child grant +
capability** for the exact payload being sent: every auto write keeps the
same hash gate, single-use consume, intent-first crash protocol, and
replayable trail as an interactive approval. The standing grant survives
(until expiry/revocation/scope change); each derived capability does not.

- Grant-less content is **skipped and recorded once** (`write-skipped`,
  per content hash — never spammed per poll, never re-asked, nothing
  sent, nothing prompted).
- Already-seen content (authorized, sent, failed, or skipped) is never
  re-authorized: a new head SHA drafts new content.
- Interactive `approve()` remains CLI-only (the structural AST test);
  `authorize_auto` is a separate gate that additionally requires a live
  standing grant and hashes the payload at issue time.
- Compensating controls for the missing human look: deterministic
  rule-drafted payloads, redaction before hashing, single-use
  capabilities, the write token's fine-grained scope, and — landing with
  #53 — the hard per-action/watch/day quota as the bound on volume.
- `readonly` stays the explicit hard-off; a fresh install has no write
  token and no grants, so the default AUTO mode is inert until the user
  acts.

## Egress when implemented

`egress.md` gains §3: GitHub writes leave only through the writer port,
carry only the approved payload bytes, use only the write token in the
Authorization header, and require a durable intent record before send.

## Acceptance — strict

Prose is not acceptance. Every invariant below is a named, offline test
(fakes, network dead, per conftest); the implementation PR does not merge
without all of them, and reviewers should treat any missing row as a
blocker, not a follow-up.

**Port-surface freeze.** A test pins the writer Protocol to exactly
`{execute}` and the capability fields to
`{action, target, content_hash, grant_id, used_at}`. Adding any write
method or capability field fails this test until this document changes.

**Gate invariants.**
- READONLY regression: `assert_allowed` raises for every write action,
  including with a write token configured.
- No capability without approval: the port dataclass is plain by design
  (#61 froze it), so the gate is the single-use consume — a capability
  the store never issued cannot pass `consume()` and is never sent.
- Silence ≠ approval: an expired request issues no capability; the
  re-request path is exercised.
- Single-use: replaying a used capability fails; concurrent double-execute
  (two threads, one capability) sends exactly once.
- Content binding: payload mutated between approval and execution → hash
  mismatch → fail closed, nothing sent, re-request required.
- Scope revocation: approve → `update_scope`/pause/cancel → capability is
  dead; execution skips, never sends.
- No self-approval: structural (AST) test — `approve(` call sites exist
  only in the CLI and tests.

**Crash protocol.** Fault-injected at three points (after intent / after
send / after outcome): a restart never re-sends; unknown state surfaces in
the inbox; the reconcile path resolves a confirmed post exactly once.

**Log completeness.** Activity-log replay of an executed write shows, in
order: draft evidence → approval request (with hash) → grant →
`write-intent` → `write-done`; the bytes at the HTTP boundary (fake
transport) hash-equal the approved hash.

**Read purity preserved.** The #38 egress tripwire and the fetch-client
no-write tests pass unchanged; a configured write token never appears on a
fetch request (Authorization-header assertion on the fake transport).

**Egress and redaction.** The write token appears in no task text, memory,
activity, inbox, or summary (byte-scan with a real configured secret).

**Dependency direction.** Writer Protocol in `ports/`; core imports it
only; the existing AST test passes.

**Live acceptance (once, user-approved).** A single real comment on an
owned repository, approved by the user through the actual CLI flow; URL
recorded; the write token then revoked/rotated. Live execution is evidence
of the deployment, not of correctness — the offline suite above is the
correctness gate.

## Explicitly out of scope

Merge, release/publish, standing (class-level) grants, model-drafted
content, GitHub App identity, webhook-driven fetching.

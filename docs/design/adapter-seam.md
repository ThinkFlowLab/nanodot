# The Adapter Seam — Port Contracts

Status: decided 2026-09-30 (issue #1 review) · implemented MVP: native runner only ·
adapters (DSH, openJiuwen are candidates): none shipped

## Governing rules

1. **Adapters execute, core decides.** The state machine, stop conditions,
   commit-pinning, notification dedup, memory, activity, and every persisted
   format live in `nanodot/core`. An adapter (alternative executor/runtime)
   can only perform work core asks for — swapping executors never changes
   behavior and never migrates data.
2. **Substitution is the proof.** The task loop's tests run entirely against
   fakes (fake clock, fake executor, fake GitHub, fake sink, fake provider).
   If test doubles can drive the loop, a future adapter can too. The
   end-to-end suite (#14) runs offline in CI with networking dead — that is
   the seam made executable.
3. **Trigger to add an adapter.** A demonstrated native-runner limitation —
   realistically, execution continuity beyond an awake host, or dispatch
   beyond one machine. Swappability alone never justifies machinery: the
   fakes already substitute every port (rule 2). The machinery-grade trigger
   is a second party extending nanodot without forking it. Until one shows
   up, this document keeps the ports coherent; no plugin machinery, adapter
   configuration, or dual-runner support is built.

## Adapter admission discipline

Borrowed from DeepSeek Harness (DSH)'s plugin rules — the discipline, not
the machinery. These bind any future adapter (DSH, openJiuwen, or other):

1. **Declared at boot, validated before state.** An adapter statically
   declares which ports it implements, what it depends on, and a config
   schema. Boot validates the declaration and fails loudly before any task
   state is read or written.
2. **Registrations are effects.** Every lock, sink, listener, or daemon a
   registration creates registers a disposer with a teardown registry;
   shutdown runs the disposers in reverse registration order. Unload leaves
   no residue — no stale lifetime locks, no orphaned notifications (#37).
3. **Provider-visible implies logged.** Nothing reaches an inference
   provider that cannot be reconstructed from the activity log. EgressGuard
   makes this structural; the egress tripwire test makes it checked. A new
   egress field must be whitelisted in docs/design/egress.md and pinned by
   that test first.
4. **Typed errors across the seam.** Port errors are typed values
   (`FetchError` variants). Core never matches on error prose, and adapter
   results are mapped into core types — no string passthrough.

## Ports

All ports live in `nanodot/ports` as small Protocols. Core depends on the
Protocols; `nanodot/native` provides the built-in implementations; tests
provide fakes; a future adapter would be another implementation.

### 1. Scheduler / executor

- **Purpose:** decide and perform "run the next check for task X now".
- **Interface sketch:** `run_once(task, now) -> RunOutcome`; a long-lived
  `serve(stop)` loop that schedules active tasks by `next_check_at`.
- **Native implementation:** an in-process polling loop with per-task
  cadence, retry backoff, and a single-flight lock (#8).
- **An adapter would implement:** running checks on another host/runtime
  (e.g. while this host sleeps) and reporting outcomes back. It would still
  call core's state machine for every decision.

### 2. GitHub snapshot fetch

- **Purpose:** return a commit-pinned view of a PR: open/merged/closed
  state, current head SHA, and complete check/status results keyed to that SHA, with known required-check
  metadata from the base branch. Unknown metadata cannot prove success.
- **Interface sketch:** `fetch(task) -> Snapshot | FetchError`
  (`FetchError` typed as retryable / auth-lost / not-found).
- **Native implementation:** read-only REST client with a read-only PAT (#6).
- **An adapter would implement:** the same reads through another transport
  (mirrors, proxies, webhooks). Write operations are not part of this port
  in any implementation.

### 3. Notification sink

- **Purpose:** deliver notable/terminal events to the user.
- **Interface sketch:** `notify(event) -> None`, idempotent per event key.
- **Native implementation:** deduplicated persisted inbox + macOS
  notification (#9). Slack/email would be further implementations.
- **An adapter would implement:** delivery through another channel; transition identity stays in core; sinks retain delivery
  deduplication by durable occurrence identity across retries and restarts.

### 4. Inference provider

- **Purpose:** the only egress point — `summarize(state_change, evidence)
  -> text` and `parse_intent(text) -> task draft`.
- **Native implementation:** API-backed model with an egress whitelist
  (PR metadata and evidence excerpts only; never credentials or store
  contents) (#11). A local model slots in behind the same interface.

### 5. Secret store

- **Purpose:** hold the GitHub PAT and inference API key; serve them to
  implementations on demand.
- **Interface sketch:** `get(name) / set(name, value) / unset(name)`.
- **Native implementation:** `0600` file inside the data home (#4).
  Redaction at every persistence boundary guarantees secrets never appear
  in task text, memory, activity, inbox, or summaries.

## Dependency direction

```
cli ──────────────┐
native ──implements──> ports <──depends-on── core
tests/fakes ──implement──┘
```

`core` imports `ports` and stdlib only (enforced by an AST test);
`native` imports `core` and `ports`; `cli` imports `core` and `native`.

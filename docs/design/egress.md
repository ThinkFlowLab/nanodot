# Egress — exactly what leaves the host

Nanodot is local-first: task state, memory, the activity log, and the inbox
live in `~/.nanodot` and never leave the machine. Two things leave, and
only through their ports:

## 1. GitHub reads (the snapshot port)

- **Destination:** `api.github.com` (read-only REST).
- **Content:** repository/PR identifiers, and the responses (check names,
  statuses, conclusions, head SHAs), applicable branch/ruleset requirements,
  and workflow event metadata. Request paths only — no POST/PUT/
  PATCH/DELETE is ever issued by the GitHub client.
- **Credential:** the read-only PAT travels in the `Authorization` header
  and exists nowhere else outside the secret store.

## 2. Inference (the provider port) — the only other egress point

Requests are built by `core.egress.EgressGuard` from a fixed whitelist;
there is structurally no way to attach anything else:

- **summarize:** `kind`, `summary` (the raw state-change message), `head_sha`,
  `pr_state`, `url`, `checks` (name + conclusion per check). I.e., public
  PR metadata and check outcomes.
- **parse_intent:** `intent_text` — the sentence the user just typed.
- **Never:** credentials, tokens, task-store contents, memory items,
  activity history, inbox contents, file paths.
- Known secret values are additionally scrubbed recursively from every outbound
  value, including check names and nested evidence. The production factory
  supplies the configured secret store to the redactor.

If the model is an API model, the above data leaves the host to that
provider; the API key travels only in the `Authorization` header. A local
model behind the same interface removes this egress entirely — no other
code changes.

## 3. GitHub writes (the writer port, gated mode)

Only in gated mode, and only behind a content-bound, single-use
capability issued by a human approval (`nanodot approvals approve`):

- **Destination:** `api.github.com` (a comment POST on the watched PR).
- **Content:** exactly the approved payload bytes — the native writer
  verifies the capability's SHA-256 against the body before sending;
  a mismatch sends nothing.
- **Credential:** the separate `github-write-token` travels only in the
  writer's `Authorization` header. It never reaches the fetch client,
  logs, memory, inbox, or summaries; the read token never reaches the
  writer. A durable `write-intent` activity record precedes every send.

Everything else — scheduling, state transitions, commit-pinning, dedup,
memory writes, redaction — happens locally.

Optional provider summaries wait at most one second in the scheduler. At most
one summary call is in flight; late responses are discarded. Native HTTP calls
use a five-second timeout. Python cannot forcibly cancel an arbitrary provider,
so a still-running call suppresses further summaries while raw events continue.

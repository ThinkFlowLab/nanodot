# Egress — exactly what leaves the host

Nanodot is local-first: task state, memory, the activity log, and the inbox
live in `~/.nanodot` and never leave the machine. Three things leave, and
only through their ports:

## 1. GitHub reads (the snapshot port)

- **Destination:** `api.github.com` (read-only REST).
- **Content:** repository/PR identifiers, and the responses (check names,
  statuses, conclusions, head SHAs), applicable branch/ruleset requirements,
  and workflow event metadata. Request paths only — no POST/PUT/
  PATCH/DELETE is ever issued by the GitHub client.
- **Credential:** the read-only PAT travels in the `Authorization` header
  and exists nowhere else outside the secret store.

## 2. Inference (the provider port)

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

**Destinations** are a registry, not a hard-wired URL (`native.providers`):
`openai-compat` (the default — any OpenAI-compatible endpoint, including
local runtimes by base-URL override), `anthropic` (Messages API), and the
`glm` / `deepseek` presets of the OpenAI-compatible protocol. The payload
whitelist above is identical for every destination; only the protocol
envelope, credential header, and endpoint differ. Selection is
`nanodot config set model-provider <name>`; the base URL is pinned and
validated (https, no fragment) once at construction, and every provider
egress — summarize or parse_intent — is recorded in the activity log
before the call (`provider-call` entries: the provider and call type,
never the user's own sentence).

If the model is an API model, the above data leaves the host to that
provider; the API key travels only in the credential header. A local
model behind the same interface removes this egress entirely — no other
code changes.

## 3. GitHub writes (the writer port)

Only in a write mode (`gated` or `auto`), only behind a content-bound,
single-use capability — issued by a human approval
(`nanodot approvals approve`) in gated mode, or derived from a standing
pre-grant in auto mode — and only while the write quota for
(action, watch, day) is not exhausted:

- **Destination:** `api.github.com` (a comment POST on the watched PR).
- **Construction invariant:** a write request exists only with a live
  grant id + whitelisted rule-drafted evidence + remaining quota; anything
  missing and no transport is constructed at all. An unanswered ask
  expires after 4 hours (silence is never approval; a second silence is a
  terminal denial).
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

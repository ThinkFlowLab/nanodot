# Egress — exactly what leaves the host

Nanodot is local-first: task state, memory, the activity log, and the inbox
live in `~/.nanodot` and never leave the machine. Two things leave, and
only through their ports:

## 1. GitHub reads (the snapshot port)

- **Destination:** `api.github.com` (read-only REST).
- **Content:** repository/PR identifiers, and the responses (check names,
  statuses, conclusions, head SHAs). Request paths only — no POST/PUT/
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
- Known secret values are additionally scrubbed from every outbound value.

If the model is an API model, the above data leaves the host to that
provider; the API key travels only in the `Authorization` header. A local
model behind the same interface removes this egress entirely — no other
code changes.

Everything else — scheduling, state transitions, commit-pinning, dedup,
memory writes, redaction — happens locally.

# nanodot tools for DSH

Topology B of the adapter study: nanodot stays a standalone, local-first CLI,
and a DSH plugin exposes that CLI surface as model-facing tools. nanodot is
not loaded into DSH and DSH is not loaded into nanodot — the seam is the
process boundary, and the protocol is nanodot's own argv/exit-code/stdout
surface (see `docs/design/adapter-seam.md`).

## What the model sees

| Tool | nanodot invocation |
|---|---|
| `nanodot_watch_add` | `watch add owner/repo#N --yes [--cadence S] [--purpose P]` |
| `nanodot_watch_list` | `watch list` |
| `nanodot_watch_show` | `watch show TASK_ID` |
| `nanodot_watch_control` | `watch pause/resume/cancel TASK_ID` |
| `nanodot_status` | `status` |
| `nanodot_inbox` | `inbox` |
| `nanodot_activity` | `activity TASK_ID` |
| `nanodot_tick` | `runner --once` |

Everything is read-only toward GitHub; watches notify on new commits, check
failures, access blockers, and terminal outcomes. Names are byte-stable
`nanodot_*` so prompt/KV-cache prefixes stay valid.

## Division of responsibility

- **Policy lives in DSH's hooks, not in these tools** (DSH's own rule).
  `nanodot_watch_add` passes `--yes` on the model's behalf, so mount this
  plugin behind a `tools/pre-execute` allow/ask policy for `nanodot_*` —
  that hook is where "a human confirms each new watch" belongs.
- **Watch semantics stay in nanodot core**: fixed validated policy,
  commit-pinning, occurrence-based notification dedup, redaction at every
  persistence boundary. Nothing here re-implements any of it.
- **Errors are typed across the seam**: `NANODOT_UNKNOWN_TOOL`,
  `NANODOT_MISSING_PARAMETER`, `NANODOT_BAD_PARAMETER`, `NANODOT_SPAWN_FAILED`,
  `NANODOT_TERMINATED`, `NANODOT_EXIT_<code>`. Bounded stderr travels as
  diagnostic data, never as the error identity.

## Mounting (per the DSH v0.1 guide)

```yaml
# dsh-nanodot.yml — mount the plugin from this checkout
- insert:
  - id: nanodot-tools
    name: /absolute/path/to/nanodot/integrations/dsh
```

```sh
dsh web --patch ./dsh-nanodot.yml
```

The plugin spawns the `nanodot` binary from `PATH`; set `NANODOT_BIN` to
point elsewhere, and `NANODOT_HOME` to select the data home (it is passed
through to the child).

## Status and tests

- `nanodot-tools.cjs` (tool table + CLI runner) is framework-free and fully
  covered offline by `npm-tests/dsh-plugin.test.cjs` (`npm test`): exact
  argument mapping, injection-safe argument handling, parameter validation,
  timeout/abort, typed errors on nonzero exit.
- `npm-tests/dsh-contract.test.cjs` drives the **real CLI** (repo source via
  a python shim, dead proxies, isolated data home) through every tool's
  offline lifecycle — add → show → activity → pause/resume/cancel → tick →
  inbox → typed `NANODOT_EXIT_1` for `status` without a runner. If the CLI's
  argument surface drifts from the tool table, this is what breaks. It needs
  python ≥ 3.11 on PATH and skips loudly otherwise.
- `index.ts` is the Cordis registration shim (~30 lines). It is written
  against the plugin API documented in the v0.1 developer-preview guide and
  has **not** been run against a live DSH yet — DSH expects breaking changes
  before 1.0, so verify the `defineTool` import when first mounting.

No nanodot core, CLI, or packaging changes are needed or made by this
integration.

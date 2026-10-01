# First useful nanodot workflow: a read-only PR watch

The useful job is: “Watch one PR. Tell me once when its current commit's CI
fails, when its head changes, and when known required checks pass. Stop after
success, merge, or closure.” Everything stays in the local task/activity/inbox
store. No model, Slack integration, or OS notification is needed.

## 1. Run the reproducible offline demonstration

Install the project from this integration branch as described in the README,
then run:

```sh
python examples/first_pr_watch.py
```

Optionally choose a **new or empty** data directory:

```sh
python examples/first_pr_watch.py --home /tmp/my-new-nanodot-demo
```

The example refuses a nonempty directory so it cannot change an existing
installation. It uses fixture HTTP responses; **no simulated transition is a
real GitHub event**. Each command launches a fresh Python process through the
production CLI parser and wiring. GitHub parsing, current-SHA evaluation,
scheduler, SQLite persistence, inbox, and runner controls are production code.
Only the HTTP boundary and a deliberate crash point are substituted.

The assertions cover:

1. Pending CI on commit A stays quiet
2. Failed CI writes one inbox entry, then the process exits abruptly before its
   task checkpoint; restarting after an unrelated optional status changes adds
   no duplicate
3. Commit B creates one new-commit notification and invalidates A's result
4. A stale successful status response for A cannot complete the watch for B
5. Successful required CI for B creates one terminal notification and completion
6. Restarting a completed watch causes no additional fetch or notification
7. Pausing excludes reads; resuming recovers; cancelling excludes future work
8. The actual background runner starts, answers status, and confirms shutdown

Expected summary: **8 scenarios passed** and exactly three inbox entries:
`checks-failed`, `new-commit`, and `checks-passed`.

The printed directory contains `demo-report.json`, `demo-transcript.txt`, the
fake-HTTP request log, and the inspectable `nanodot.db`. No credentials are saved.
The script leaves no runner active. It is also part of the offline test suite.
The crash assertion proves inbox deduplication for this replay boundary, even
when optional-check evidence changes before recovery. The offline tests also
exercise this boundary for terminal success. This is not a blanket guarantee
about arbitrary filesystem corruption or OS notifications.

## 2. Try a real public PR without credentials

Use a new shell or preserve the temporary data path if you want to revisit it:

```sh
export NANODOT_HOME="$(mktemp -d)"
nanodot config set github-auth-mode anonymous
nanodot config set os-notifications false
nanodot watch add ThinkFlowLab/nanodot#27 --cadence 1800 --yes
nanodot runner --once
nanodot watch list
nanodot inbox
nanodot runner --once
```

PR #27 was already merged when this guide was validated. That makes it a quick
real-network smoke test: the first pass records one merged notification and
completes the watch; the second pass reports `ran 0 task(s)`. This observes an
existing terminal state; it does **not** claim to have watched the merge happen
or certify CI.

For ongoing use, substitute an open PR you care about. The scope follows the
PR's latest head and evaluates that exact SHA. With anonymous access, required
check metadata may be hidden; such a watch remains pending rather than claiming
success. If no required checks are configured, it stays active until the PR
merges or closes, even when optional CI is green. Use `activity TASK_ID` to see
why a watch is pending.

A normal snapshot can consume several API requests, more with multiple check
suites/pages. Use a conservative cadence, inspect rate-limit errors in activity,
and avoid creating many anonymous watches. Rate limits retry with backoff.
Anonymous mode never sends a saved token or silently falls back to it. Default
`token` mode still requires an explicitly configured existing credential. Stop
the runner before changing or unsetting `github-auth-mode` or
`os-notifications`, then start it again. The CLI rejects these configuration
changes while a runner is active so a successful change cannot leave the live
runner using its old authentication or desktop-notification setting.

## 3. Leave it running, inspect it, and stop it

```sh
nanodot start
nanodot status
nanodot watch list
nanodot watch show TASK_ID
nanodot activity TASK_ID
nanodot inbox
nanodot watch pause TASK_ID
nanodot watch resume TASK_ID
nanodot watch cancel TASK_ID
nanodot stop
nanodot status
```

Copy the task ID printed by `watch add`. Pause then resume schedules an immediate
poll; resume of an already-active watch is idempotent. Cancel permanently stops
that watch. `stop` stops the runner but keeps active tasks for a future `start`.
A completed/cancelled watch cannot be resumed. Runner shutdown is cooperative:
an in-flight request may finish before it stops; success is reported only after
the runner releases its lease. `status` returns exit code 1 when stopped.

`os-notifications false` is a real boolean and disables desktop notifications;
the local inbox remains enabled. No inference provider is configured in these
examples. Do not put tokens in command arguments or save them for this public
example.

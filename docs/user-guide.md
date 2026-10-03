# User guide

This guide covers the PR-watch MVP on the default `main` branch. Set it up
with the [installation guide](installation.md); no branch switching is needed.

The MVP watches GitHub pull requests, keeps a local notification inbox and
activity history, and stores memory you can inspect and edit. It runs on Linux
and macOS. A model is optional; the normal PR-watch workflow works without one.

## Try it offline first

With your virtual environment active, run this from the checkout:

```sh
python examples/first_pr_watch.py
```

The example uses simulated GitHub responses with the real CLI, runner, and
database. It needs no GitHub token, model, or network access. It exercises
pending and failed CI, a new commit, stale results, successful required checks,
restart recovery, cancellation, and background runner shutdown.

The final output should include `8 scenarios passed` and the directory holding
the report and command transcript. It leaves your normal nanodot data alone and
stops its runner. See the
[detailed walkthrough](first-pr-watch.md)
for the individual scenarios.

## Choose where to keep your data

By default, nanodot uses `~/.nanodot`. To keep a separate installation, set a
stable path before running any commands:

```sh
export NANODOT_HOME="$HOME/.nanodot-pr-watch"
```

Repeat that export in new terminals when using this installation. All commands,
including `start`, `status`, and `stop`, must use the same data home to refer to
the same tasks and runner. If you leave `NANODOT_HOME` unset, they use the default.

| File in the data home | Contents |
| --- | --- |
| `nanodot.db` | Tasks, memory, activity, inbox, and permission records. |
| `config.json` | Non-secret configuration. |
| `secrets.json` | Saved credentials, in a local file with `0600` permissions. |
| `runner.log` | Background runner output. |

Runtime lock/control files also live here. Keep this directory private. Saved
credentials are plaintext in a permission-restricted file; they are not an
encrypted keychain. Uninstalling the Python package does not remove your data.

## Watch a public pull request

For a public PR, explicitly choose anonymous access. Stop any runner for this
data home before changing its authentication or desktop notification settings:

```sh
nanodot stop
nanodot config set github-auth-mode anonymous
nanodot config set os-notifications false
nanodot watch add 'owner/repo#123' --cadence 1800
```

Replace `owner/repo#123` with the PR you want to follow. `watch add` previews the
scope and asks `Proceed? [y/N]`. Answer `y`, then copy the printed task ID for
commands below. Use `--yes` to skip confirmation in scripts.

Run the first check yourself and inspect the result:

```sh
nanodot runner --once
nanodot watch list
nanodot inbox
```

`runner --once` runs one pass over tasks whose next check is due. Creating a
watch makes it due immediately. Subsequent passes before its next check may
print `ran 0 task(s)`; they do not force an early poll.

Anonymous mode never sends a saved token. GitHub may hide required-check rules
from anonymous callers, and anonymous API limits are smaller. A 1,800-second
cadence means checking every 30 minutes; the default is 300 seconds. A snapshot
can require several requests. Inspect activity if GitHub reports rate limits.

An open watch can produce no notification yet: pending CI stays quiet. A watch
on an already merged or closed PR records that terminal state and completes
when checked.

## Use authenticated access

For private repositories or authenticated reads, use an existing token with
read access to the repository and the required metadata:

```sh
nanodot stop
nanodot config set github-auth-mode token
nanodot config set github-token
nanodot config list
```

The token command prompts without echoing your input; secret values are masked
in `config list`. For noninteractive entry, `nanodot config set github-token -`
reads one line from stdin. Avoid putting literal credentials in arguments,
where shell history and process listings can retain them.

Token mode is the default. The GitHub client reads PRs, checks, commit statuses,
branch/ruleset requirements, and Actions workflow metadata. The token needs
access to those resources. Some branch-protection metadata requires
Administration **read** permission; nanodot does not need write access. Hidden
or inaccessible requirements cannot be treated as a successful check result.

Create a watch as above, or repair an existing blocked watch and resume it:

```sh
nanodot watch resume TASK_ID
nanodot runner --once
```

Replace `TASK_ID` with your saved task ID. Resuming a blocked or paused watch
schedules an immediate check. If you previously used a background runner,
restart it with `nanodot start` after changing settings.

## Keep watching in the background

```sh
nanodot start
nanodot status
```

`start` launches one background runner for this data home. You can close the
terminal, but the host must remain awake and able to reach GitHub. It is not a
system service and does not install automatic startup after a reboot. Run
`nanodot start` again when needed; saved active watches remain in the database.

For a runner attached to your terminal, use `nanodot runner` and press Ctrl-C
to stop it. A foreground runner, background runner, and one-shot pass all share
the same lock; only one can run for a data home at a time.

To stop the background runner:

```sh
nanodot stop
nanodot status
```

Stopping the runner preserves active watches for the next start. Shutdown waits
for in-flight work to finish. If a stop times out, check status and retry; the
stop request remains pending. `status` exits with code 0 when running and 1
when stopped, so a stopped runner's exit code is expected.

## Inspect and manage watches

Replace `TASK_ID` in these commands with the ID from `watch add` or `watch list`.

| Command | Purpose |
| --- | --- |
| `nanodot watch list` | Show task IDs, states, next checks, and blockers. |
| `nanodot watch show TASK_ID` | Inspect the saved target, scope, and recent activity. |
| `nanodot activity TASK_ID` | Read recent activity for one watch. |
| `nanodot activity` | Read recent activity across tasks. |
| `nanodot inbox` | Read persisted notifications. |
| `nanodot watch pause TASK_ID` | Suspend future checks for this watch. |
| `nanodot watch resume TASK_ID` | Reactivate a paused/blocked watch and make it due now. |
| `nanodot watch cancel TASK_ID` | Permanently cancel this watch. |

Watch states are `active`, `paused`, `blocked`, `completed`, and `cancelled`.
Completed or cancelled watches remain inspectable but cannot be resumed;
create a new watch instead. Cancelling one watch leaves the runner available
for other tasks.

### What a watch reports

The MVP has a fixed policy: notify on new commits, observed current-head check
failures, access blockers, and terminal outcomes. Complete when known required
checks pass on the current head, or when the PR merges or closes. A new commit
invalidates the old commit's CI result.

A check-pass result requires a complete snapshot and known required checks for
the base branch. Unknown or empty requirements, missing required checks,
neutral/skipped results, and unsupported rules can leave the watch active even
when the GitHub page looks green. Optional failures notify while required
success is unconfirmed; they do not prevent a confirmed required-check pass.
Use `activity TASK_ID` to inspect what nanodot actually observed.

This checks current-head CI, not every condition for mergeability. It does not
evaluate a separate merge-queue commit. `--purpose` adds a description; it does
not change the policy. Custom natural-language `--notify` or `--stop` conditions
are rejected.

## Notifications

The local inbox works on Linux and macOS. macOS also supports desktop
notifications through `osascript`; Linux users should read `nanodot inbox`.
To change desktop notifications:

```sh
nanodot stop
nanodot config set os-notifications true
nanodot start
```

Use `false` to disable them. The default is `true`; the inbox stays enabled
either way. Authentication mode and desktop notification changes are rejected
while a runner is active, so stop it before setting or unsetting those values.

## Add and edit memory

```sh
nanodot memory add 'prefer morning deploys'
nanodot memory list
nanodot memory show ITEM_ID
nanodot memory edit ITEM_ID --content 'prefer afternoon deploys'
nanodot memory rm ITEM_ID
```

Replace `ITEM_ID` with the ID printed by `memory add` or `memory list`. Directly
added statements are confirmed. Use `--kind fact` or `--kind observation` when
adding an item to choose those kinds; the default is `preference`.

To record a suggestion that still needs confirmation:

```sh
nanodot memory propose 'review after lunch'
nanodot memory confirm ITEM_ID
```

Use the proposal's own ID for confirmation. Unconfirmed proposals expire after
14 days. Relevant confirmed memory can appear in a watch's creation preview,
but it does not change the fixed watch policy. Deletion leaves a contentless
activity record; it does not erase copies in external backups.

## Optional model configuration

Skip this section to use explicit PR targets without a model. To enable intent
parsing and optional notification summaries, configure an OpenAI-compatible
provider:

```sh
nanodot config set api-key
nanodot config set model-base-url https://YOUR_PROVIDER/v1
nanodot config set model-name YOUR_MODEL
nanodot watch add --intent 'watch owner/repo#123 until required checks pass'
```

Replace the provider URL, model name, and PR target with your values. The API
key uses a hidden prompt. The model helps parse the target and purpose; it does
not enable arbitrary watch conditions or external writes.

Your explicit intent text and selected PR/check metadata can be sent to this
provider. Stored memory and local activity history are not included. Read the
[egress contract](design/egress.md)
before enabling it. To disable inference, run `nanodot config unset api-key`.

## Permissions

```sh
nanodot approvals
nanodot approvals grant --action comment --target owner/repo#N
nanodot approvals approve <request-id>
nanodot approvals deny <request-id>
```

`nanodot approvals` shows the current mode, pending requests, and active
grants. The default `auto` mode never asks: a comment on a failing watched
PR is sent only when a standing pre-grant exists (created by
`approvals grant`, 7-day default expiry, `--no-expiry` to opt out);
otherwise the write is skipped and recorded once in the inbox. `gated`
mode (`nanodot config set permission-mode gated`) pauses every write as a
proposal until you approve it; an unanswered proposal expires after 4
hours, is re-asked at most once, and a second silence is recorded as a
denial. Write volume is capped per watch per day (3 comments by default;
when the budget is spent the write is skipped and recorded, and the watch
keeps running). `readonly` (`nanodot config set permission-mode readonly`)
proposes and sends nothing. All writes additionally require
`nanodot config set github-write-token`.

## Troubleshooting usage

| Symptom | Next step |
| --- | --- |
| `watch` or `memory` is an unrecognized command | Update the checkout (`git pull`) and reinstall as described in the [installation guide](installation.md). |
| `no GitHub token configured` | Select `anonymous` for public PRs, or save a read-only token for token mode. |
| Watch is blocked after token/access loss | Repair repository access, inspect `watch show TASK_ID`, then run `watch resume TASK_ID`. |
| CI looks green but the watch remains active | Inspect `activity TASK_ID`; required rules may be hidden, empty, missing, or unsupported. |
| `ran 0 task(s)` | Check `watch list`: no active watch is due yet, or the watch has completed. |
| Runner is already running | Use the existing runner, or stop it before a foreground/one-shot run. |
| Tasks appear to be missing | Check that this terminal uses the same `NANODOT_HOME` as the original one. |
| No desktop alert appears | Read `inbox`, check `os-notifications`, and remember desktop delivery is macOS-only. |
| Runner fails to start or stop | Read `runner.log` in your selected data home and check `nanodot status`. |

For command-specific help:

```sh
nanodot watch add --help
nanodot memory --help
nanodot config --help
```

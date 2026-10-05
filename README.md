# nanodot

A minimal, local-first Python assistant with persistent memory and a read-only
GitHub PR watcher. The native CLI and runner work without an inference provider.
Linux and macOS are supported; OS notifications use macOS `osascript`, with a
persistent inbox available on either platform.

## Install and run

Python 3.11 or newer is required. From this checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
nanodot --help
```

## Install with npm on macOS or Linux

Requires Node.js 22 or newer. After the first npm release is published:

```sh
npm install -g nanodot
nanodot --help
```

Or run without a global installation:

```sh
npx nanodot --help
```

The launcher uses an existing Python 3.11+ when available. Otherwise, its first
run downloads a private Python runtime automatically. macOS (Intel and Apple
Silicon) and common Linux distributions (x86_64 and aarch64) are supported.
Automatic setup needs internet access and `curl` plus `tar`, which are included
with macOS and common Linux distributions; on minimal container images, install
them or Python 3.11+ first. Later runs reuse the installed runtime.

Runtime downloads live in `~/Library/Caches/nanodot/npm` on macOS and
`~/.cache/nanodot/npm` on Linux; `XDG_CACHE_HOME` overrides the cache location.
Application data uses `~/.nanodot`, or the directory set by `NANODOT_HOME`.

See [npm packaging and release checks](docs/npm-packaging.md) for testing a
package before publication.

## First use: watch a public PR

Try the complete offline lifecycle first (no token, model, or network):

```sh
python examples/first_pr_watch.py
```

It runs eight scenarios through the real CLI in separate processes, including
an actual crash/restart, inbox deduplication, stale-commit rejection, and stop.
The PR transitions are simulated; the CLI, native parsing, runner and database
are real. The output includes a report and command transcript.

For a real public PR, use an isolated data home and explicit anonymous access:

```sh
export NANODOT_HOME="$(mktemp -d)"
nanodot config set github-auth-mode anonymous
nanodot config set os-notifications false
nanodot watch add owner/repo#123 --cadence 1800 --yes
nanodot runner --once
nanodot watch list
nanodot inbox
```

Anonymous mode never sends a GitHub token, even if one is saved. Stop the runner
before changing authentication mode or OS-notification settings, then restart
it; live changes to those settings are rejected. Public metadata
can be restricted and has a smaller API quota; 30-minute polling is a cautious
starting point. Unknown required-check rules cannot complete a watch, but
observed current-head failures still notify. No token or model is needed. See the [first-use walkthrough](docs/first-pr-watch.md) for the full
lifecycle, background runner, cancellation, and verification details.

For private repositories or authenticated reads, keep the default token mode
(or set `github-auth-mode token`) and configure an existing read-only token with
a hidden terminal prompt:

```sh
nanodot config set github-token
```

For noninteractive entry, `nanodot config set github-token -` reads one line
from stdin. Avoid literal secrets in command arguments: they can appear in shell
history and process listings. nanodot does not create tokens or expand grants.

The GitHub adapter reads PRs, check suites/runs, commit statuses, applicable
branch/ruleset metadata, and GitHub Actions workflow metadata. The token must be
able to read those resources for the repository. Some classic protection
metadata requires Administration **read** permission. Missing access fails
closed; never grant write permission just to use a watcher.

```sh
nanodot watch add owner/repo#123
nanodot watch list
nanodot watch show TASK_ID
nanodot runner --once
nanodot start
nanodot status
nanodot inbox
nanodot activity TASK_ID
nanodot watch pause TASK_ID
nanodot watch resume TASK_ID
nanodot watch cancel TASK_ID
nanodot stop
```

`watch add` previews the saved scope and asks for confirmation (`--yes` skips
that prompt). The default cadence is 300 seconds; `--cadence` changes it. Fix a
lost token before resuming a blocked watch. Resume schedules it immediately.
Cancelled and completed watches cannot be restarted; create a new one instead.
Only one runner may hold a data home's lifetime lock. Shutdown is cooperative;
a timeout reports that stopping is still pending rather than signaling a saved
PID or claiming the process exited.

### The activity log is a decision log

Every poll records what the runner observed (`check-observed` entries) before
any events it produced, and every event's evidence names the policy rule that
fired (`stop:`, `notify:`, `record:`). A run overtaken by a pause, cancel, or
scope change records nothing. Replaying the log reproduces every stop/notify
decision without re-asking GitHub. The default `nanodot activity` view hides
per-poll observations so events stay readable; pass `--all` to include them.

### Writes: auto by default, gated for interactive approval

The watcher can propose one external action — a comment on the PR it
watches, rule-drafted from observed check failures. Every write goes
through a single-use, content-bound capability: the bytes at the HTTP
boundary must hash-match what was authorized, and a crash mid-write
surfaces as unknown in the inbox and is never blindly re-sent.

`auto` (the default) never prompts: writes execute only against a
standing pre-grant you create explicitly (`nanodot approvals grant
--action comment --target owner/repo#N`, 7-day default expiry);
grant-less content is skipped and recorded once, nothing sent, nothing
asked. `gated` pauses every write as a proposal in the inbox until you
run `nanodot approvals approve <request-id>`; a proposal nobody answers
expires in 4 hours, is re-asked at most once, and a second silence is
recorded as a denial — never re-asked verbatim. `readonly` is the
explicit hard-off — no writes are even proposed. All write modes require a separate write token
(`nanodot config set github-write-token`); the read token never gains
write reach, and without a write token every mode is inert.

Writes are also bounded by a hard daily budget per watch — by default
3 comments, 3 labels, and 1 review approval per day (the comment action
is the first shipped). Exhaustion is recorded once in the activity log
and the action is simply not attempted again until the next day; it is
never an error for the watch loop.

### Scheduled digest

`watch add --digest {6h,12h,24h}` adds a fixed-interval status heartbeat to
a watch: one inbox digest per window on a healthy poll — PR state, head,
checks on head — even when nothing changed. The first digest fires only
after one full interval; terminal, failing, paused, or cancelled ticks
never digest; a crash-replay within a window cannot double-deliver.

### Stale alerts

`watch add --stale {2d,3d,7d,14d}` adds a once-per-commit idle alert: when a
watched PR's head SHA sits unchanged for the configured days, exactly one
notification fires (days idle, PR state, head, checks). A new commit re-arms
it; terminal, failing, paused, or cancelled ticks never alert. Composes
freely with `--digest`.

### Flaky alerts

`watch add --flaky` notifies when a check flips between red and green on the
same commit — whether the rerun is visible within one poll or across polls.
Each flip is one notification (distinct occurrences, so flapping stays
visible); non-definitive states never count; a new commit resets. Composes
freely with `--digest` and `--stale`.

### Fixed watch policy

This MVP supports a fixed, validated policy. It notifies on new commits, check
failures, access blockers, and terminal outcomes. It stops when the required
checks pass on the current head, or the PR merges or closes. `--notify` and
`--stop` accept the supported policy text (including the original shipped
defaults) only; arbitrary natural-language conditions are rejected. The
`--purpose` text is descriptive and does not change execution. Unsupported
legacy rows remain inspectable/cancellable and are blocked before fetching.

A passing result requires a complete paginated snapshot and known required
check rules for the PR's base branch. Missing required contexts, stale commits,
unknown rules, or inaccessible metadata cannot satisfy a watch. Legacy commit
statuses are included; latest reruns are evaluated without mixing app sources.
Observed current-head failures, including optional checks, notify while required
success is unconfirmed. Optional failures do not block a confirmed required-check pass.

Conservative limits:

- No configured required checks keeps a watch active until the PR closes or
  merges; optional green checks are not treated as proof of a requirement.
  Observed current-head failures still notify when rules are empty or hidden
- Neutral/skipped results stay pending; nanodot requires literal success
- App-bound legacy statuses cannot be proven from REST creator identity, so
  they stay pending when app provenance cannot be verified
- Unsupported rule types (for example merge queues or required workflows) and
  unresolved relevant suites stay pending
- This watches current-head CI; it does not certify full mergeability or
  evaluate the separate merge-queue/merge-test commit

## Optional inference

```sh
nanodot config set api-key
nanodot config set model-base-url https://api.openai.com/v1
nanodot config set model-name YOUR_MODEL
nanodot watch add --intent 'watch owner/repo#123 until required checks pass'
```

With a configured provider, the explicit intent text and whitelisted PR/check
metadata can be sent to that provider. Memory contents and local history are
not included. Known stored secret values are scrubbed from all outbound fields;
credentials travel only in authorization headers. Review
[the egress contract](docs/design/egress.md) before enabling it. Optional
summaries have a one-second scheduler wait budget and at most one in-flight
request; late/error responses are discarded and raw notifications still work.

## Memory and permissions

```sh
nanodot memory add 'prefer morning deploys'
nanodot memory propose 'review after lunch'
nanodot memory list
nanodot memory show ITEM_ID
nanodot memory confirm ITEM_ID
nanodot memory edit ITEM_ID --content 'prefer afternoon deploys'
nanodot memory rm ITEM_ID
nanodot approvals
```

User statements are confirmed; proposals need explicit confirmation and expire
after 14 days. Expiry is enforced when opening/reading/confirming memory.
Terminal observations are best-effort after durable task completion. Deletion
removes the item, securely overwrites deleted SQLite cells, and leaves a
contentless activity tombstone. This is not a promise to erase filesystem
snapshots, backups, or copies retained outside nanodot.

All three permission modes are implemented: `auto` (the default;
standing pre-granted capabilities only, never prompts), `gated`
(interactive approval per write), and `readonly` (the hard-off). Grants
and approval records remain inspectable; scope changes revoke related
grants and pending requests atomically.

Data defaults to `~/.nanodot`; `NANODOT_HOME` selects an isolated directory.
Secrets are stored in a private `0600` file via atomic replacement with symlink
checks. SQLite holds task state, memory, activity, and inbox entries. Existing
plaintext secret-named config values are masked on listing and removed when
reset/unset. A process killed during an atomic save may leave a private temporary
file; system backups and shell history remain outside these guarantees.

## Test offline

Install dependencies first, then run:

```sh
HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
ALL_PROXY=http://127.0.0.1:9 NO_PROXY= python -m pytest -q
```

The test fixture rejects in-process IP sockets and DNS resolution, while native
transports are replaced by fakes. Dead proxy settings are inherited by child
processes. This is a test tripwire, not an OS firewall sandbox for arbitrary
subprocesses. CI applies offline proxy settings to the test step only, after
checkout, Python setup, and dependency installation.

## The loopback UI

```sh
nanodot ui
```

A single-page interface served on `127.0.0.1` only (a random token is
generated per boot; the address is printed and opened in the browser).
Tasks, inbox, activity, memory, and approvals render CLI output; the
runner can be started and stopped from the page. The UI is an
unprivileged consumer like any other: every mutation spawns the CLI in a
child process — there is no second writer and no new egress. See
[RFC #100](https://github.com/ThinkFlowLab/nanodot/issues/100) for the
design and the roadmap (approvals surface, first-run wizard, desktop
shell).

## Parallel work: claim before you start

This repository is developed by multiple concurrent agent sessions, and a
green CI run proves a PR's code — not that two PRs agree with each other.
Before starting work on an issue, comment a claim on it
(`claiming — starting now`), and check open PRs and recent claims first.
If a claim has gone stale (no activity for a few hours), it is free again.
When a collision is discovered late, the later PR closes as a duplicate
with a diff of anything unique it found — see #71 and #89 for the pattern.
CI cannot catch semantic conflicts; the claim protocol is the defense.

## Documentation

- [Installation](docs/installation.md): prerequisites, source setup, updates,
  development setup, and installation troubleshooting.
- [User guide](docs/user-guide.md): authentication, first PR watch, runner,
  notifications, task management, memory, optional models, and troubleshooting.
- [First-use walkthrough](docs/first-pr-watch.md): the full watch lifecycle,
  background runner, cancellation, and verification details.
- [npm packaging](docs/npm-packaging.md): installing via npm on macOS or Linux,
  and testing a package before publication.

See [adapter contracts](docs/design/adapter-seam.md) for dependency direction and
[the safety/validation review](docs/design/safety-validation.md) for the review
coverage and integration plan.

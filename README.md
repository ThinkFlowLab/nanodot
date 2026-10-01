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
starting point. Unknown required-check rules stay pending. No token or model is
needed. See the [first-use walkthrough](docs/first-pr-watch.md) for the full
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
Optional failures do not block a confirmed required-check pass.

Conservative limits:

- No configured required checks keeps a watch active until the PR closes or
  merges; optional green checks are not treated as proof of a requirement
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

Only `readonly` mode is implemented. `gated`/`auto` cannot be enabled, and all
write actions are denied. Grants and approval records remain inspectable;
scope changes revoke related grants and pending requests atomically.

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

See [adapter contracts](docs/design/adapter-seam.md) for dependency direction and
[the safety/validation review](docs/design/safety-validation.md) for the review
coverage and integration plan.

# nanodot

nanodot is a minimal, open-source AI assistant, designed for persistent memory and proactive task execution.

The full PR-watch MVP is currently in
[PR #28](https://github.com/ThinkFlowLab/nanodot/pull/28). It includes a read-only
GitHub watcher, a background runner, a local inbox, and editable memory. The
current `main` branch contains the CLI scaffold with help and version output.

## Install and try the MVP

Use Linux or macOS with Python 3.11 or newer and Git:

```sh
git clone --branch integration/mvp-first-pr-watch https://github.com/ThinkFlowLab/nanodot.git
cd nanodot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
nanodot --help
python examples/first_pr_watch.py
```

The example demonstrates the PR-watch lifecycle offline, using simulated
GitHub responses. It needs no token or model and should finish with
`8 scenarios passed`.

To install the scaffold on `main`, omit `--branch integration/mvp-first-pr-watch`
from the clone command and verify it with `nanodot --help` and
`nanodot --version`; the demo and watch commands require the MVP branch.

## Documentation

- [Installation](docs/installation.md): prerequisites, source setup, updates,
  development setup, and installation troubleshooting.
- [User guide](docs/user-guide.md): authentication, first PR watch, runner,
  notifications, task management, memory, optional models, and troubleshooting.
- [Adapter contracts](docs/design/adapter-seam.md): the project's dependency
  boundaries and planned runtime ports.

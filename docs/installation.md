# Installation

## Requirements

- Linux or macOS for the PR-watch MVP.
- Python 3.11 or newer, with `pip` and `venv` available.
- Git to download and update the source.

The commands below use a POSIX shell on Linux or macOS. Python 3.12 is the
version used in CI. Installation needs network access to download the source
and build dependencies.

## Install from source

Clone the repository:

```sh
git clone https://github.com/ThinkFlowLab/nanodot.git
cd nanodot
```

Choose the version you want to install **before** creating the environment:

| Checkout | Available functionality |
| --- | --- |
| `main` (the default clone) | CLI scaffold: help and version output. |
| `integration/mvp-first-pr-watch` | PR watching, runner, inbox, activity, memory, and optional inference. |

The full MVP is currently in [PR #28](https://github.com/ThinkFlowLab/nanodot/pull/28).
To follow the [user guide](user-guide.md), switch to that branch:

```sh
git switch integration/mvp-first-pr-watch
```

Then create and activate the environment, and install nanodot:

```sh
python3 --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

The Python version printed above must be at least 3.11. If your system's
`python3` is older, use an installed newer interpreter, such as `python3.12`,
to create the environment instead.

The editable installation reads code from this checkout. Keep the checkout in
place while using this environment. You do not need the development
dependencies just to run nanodot.

Check that the command is available:

```sh
nanodot --version
nanodot --help
```

The version command prints `nanodot 0.1.0`. Help shows the commands available
in the installed checkout.

If help only lists `--help` and `--version`, you installed the `main` scaffold.
To use the MVP commands, switch to the integration branch above and rerun
`python -m pip install -e .`.

Next, follow the [user guide](user-guide.md). The MVP's offline demonstration
does not need a GitHub token, a model, or network access after installation.

## Open a new terminal

Activate the same environment before running nanodot:

```sh
cd /path/to/nanodot
. .venv/bin/activate
nanodot --help
```

Replace `/path/to/nanodot` with your clone's location. You can run nanodot from
any directory after activation. To leave the environment, run `deactivate`.

## Update an installation

For an MVP installation, stop the runner before updating. Use the same
`NANODOT_HOME` you use when starting it:

```sh
nanodot stop
```

From your checkout, with its environment active, update the source and install:

```sh
git pull --ff-only
python -m pip install -e .
nanodot --version
nanodot --help
```

`git pull` updates the branch you selected during installation. For the MVP,
run `nanodot start` afterward if you want background checking to resume.

If Git reports local changes or a diverged branch, resolve that situation
before updating; do not discard work to force the update. The package version
may stay the same between source commits, so help is also useful for checking
which commands your checkout supports.

## Development setup

To install the test dependencies as well:

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
```

## Troubleshooting installation

| Symptom | What to check |
| --- | --- |
| Python version is below 3.11 | Create the environment with a newer Python interpreter. |
| `No module named venv`, or `ensurepip` is unavailable | Install the `venv`/`pip` support for your Python using your OS's Python packaging instructions, then recreate the environment. |
| `nanodot: command not found` | Activate `.venv` and rerun `python -m pip install -e .` from the checkout. |
| `No module named nanodot` | Check that you are using the environment in which you installed the project. |

When diagnosing which interpreter and command you are using, run:

```sh
python -c 'import sys; print(sys.executable)'
command -v nanodot
python -m pip show nanodot
```

You can also invoke the CLI through the active interpreter:

```sh
python -m nanodot.cli --help
```

This uses the same commands as the `nanodot` console script.

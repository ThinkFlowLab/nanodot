"""Command-line interface. CLI/TUI is the first interface per the issue #1
review; a local web UI can layer on the same core later.

The four UX questions from issue #1 become commands:
  Tasks    -> nanodot watch ...
  Activity -> nanodot activity / inbox
  Memory   -> nanodot memory ...      (issue #12)
  Approvals-> nanodot approvals       (issue #13)
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from nanodot import __version__
from nanodot.paths import data_home

DEFAULT_CADENCE = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanodot",
        description="A minimal, local-first AI assistant with persistent memory "
        "and proactive task execution.",
    )
    parser.add_argument("--version", action="version", version=f"nanodot {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # -- config ----------------------------------------------------------
    config = subparsers.add_parser("config", help="view or set configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set", help="set a value")
    config_set.add_argument("name")
    config_set.add_argument("value")
    config_unset = config_sub.add_parser("unset", help="remove a value")
    config_unset.add_argument("name")
    config_sub.add_parser("list", help="list configured values (secrets masked)")

    # -- watch (Tasks) -----------------------------------------------------
    watch = subparsers.add_parser("watch", help="task inbox: PR watches")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    add = watch_sub.add_parser("add", help="create a watch on one PR")
    add.add_argument("target", help="owner/repo#number")
    add.add_argument("--purpose", default="tell me when required checks pass")
    add.add_argument("--cadence", type=int, default=DEFAULT_CADENCE,
                     help="seconds between checks (default 300)")
    add.add_argument("--notify", default="check failures and terminal outcomes")
    add.add_argument("--stop", default="required checks pass on the current head "
                                       "SHA, or the PR merges or closes")
    add.add_argument("--yes", action="store_true", help="skip confirmation")
    watch_sub.add_parser("list", help="status, latest result, next check, blockers")
    show = watch_sub.add_parser("show", help="full saved scope of one task")
    show.add_argument("task_id")
    for action in ("pause", "resume", "cancel"):
        cmd = watch_sub.add_parser(action, help=f"{action} a task")
        cmd.add_argument("task_id")

    # -- activity / inbox --------------------------------------------------
    activity = subparsers.add_parser("activity", help="what actually ran")
    activity.add_argument("task_id", nargs="?")
    subparsers.add_parser("inbox", help="notifications received")

    # -- runner ------------------------------------------------------------
    runner = subparsers.add_parser("runner", help="the background checker")
    runner.add_argument("--once", action="store_true",
                        help="run one scheduler pass and exit")
    subparsers.add_parser("start", help="start the background runner")
    subparsers.add_parser("stop", help="stop the background runner")
    subparsers.add_parser("status", help="is the background runner running?")

    return parser


# -- shared wiring -----------------------------------------------------------


def _wiring() -> tuple:
    from nanodot.core.activity import ActivityLog
    from nanodot.core.redaction import Redactor
    from nanodot.core.runner import TaskLoop
    from nanodot.core.tasks import TaskStore
    from nanodot.native.github_client import GitHubSnapshotFetcher
    from nanodot.native.notifier import NativeNotifier
    from nanodot.native.secrets_file import FileSecretStore

    secrets = FileSecretStore()
    redactor = Redactor(secrets)
    store = TaskStore(redactor=redactor)
    activity = ActivityLog(redactor=redactor)
    sink = NativeNotifier(redactor=redactor, os_notify=_os_notifications_enabled())
    fetcher = GitHubSnapshotFetcher()
    loop = TaskLoop(store, fetcher, sink, activity)
    return secrets, store, activity, sink, fetcher, loop


def _os_notifications_enabled() -> bool:
    from nanodot.core.config import Config

    return bool(Config().get("os-notifications", True))


def _fmt_time(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# -- config -------------------------------------------------------------------


def _run_config(args: argparse.Namespace) -> int:
    from nanodot.core.config import Config
    from nanodot.core.redaction import MASK, is_secret_name
    from nanodot.native.secrets_file import FileSecretStore

    config = Config()
    store = FileSecretStore()
    if args.config_command == "set":
        if is_secret_name(args.name):
            store.set(args.name, args.value)
        else:
            config.set(args.name, args.value)
    elif args.config_command == "unset":
        if is_secret_name(args.name):
            store.unset(args.name)
        else:
            config.unset(args.name)
    else:  # list
        rows = [(key, str(config.get(key))) for key in config.keys()]
        rows += [(name, MASK) for name in store.names()]
        for key, value in sorted(rows):
            print(f"{key}={value}")
    return 0


# -- watch ---------------------------------------------------------------------


def _run_watch(args: argparse.Namespace) -> int:
    from nanodot.core.tasks import PRTarget, Task, TaskError
    from nanodot.native.secrets_file import FileSecretStore

    secrets, store, activity, sink, _, _ = _wiring()

    if args.watch_command == "add":
        if not secrets.get("github-token"):
            print(
                "error: no GitHub token configured — run: "
                "nanodot config set github-token <read-only PAT>",
                file=sys.stderr,
            )
            return 1
        try:
            target = PRTarget.parse(args.target)
        except TaskError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        task = Task(
            target=target,
            purpose=args.purpose,
            cadence_seconds=args.cadence,
            notification_conditions=args.notify,
            stop_conditions=args.stop,
            next_check_at=time.time(),
        )
        print("About to create a watch:")
        print(f"  target:                 {task.target}")
        print(f"  purpose:                {task.purpose}")
        print(f"  cadence:                every {task.cadence_seconds}s")
        print(f"  allowed actions:        read-only (no external writes)")
        print(f"  notification conditions:{task.notification_conditions}")
        print(f"  stop conditions:        {task.stop_conditions}")
        if not args.yes:
            answer = input("Proceed? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("cancelled")
                return 1
        created = store.create(task)
        print(f"created watch {created.id} ({created.target})")
        return 0

    if args.watch_command in ("pause", "resume", "cancel"):
        try:
            task = getattr(store, args.watch_command)(args.task_id)
        except TaskError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"{args.watch_command}d {task.id} ({task.target})")
        return 0

    if args.watch_command == "list":
        tasks = store.list()
        if not tasks:
            print("no tasks — create one with: nanodot watch add owner/repo#1")
            return 0
        for task in tasks:
            latest = activity.query(task_id=task.id, limit=1)
            latest_text = latest[0].message if latest else "-"
            if len(latest_text) > 60:
                latest_text = latest_text[:57] + "..."
            blocker = f"  [blocked: {task.blocker}]" if task.blocker else ""
            print(
                f"{task.id}  {task.target}  {task.state.value:<9} "
                f"next={_fmt_time(task.next_check_at)}  {latest_text}{blocker}"
            )
        return 0

    # show
    task = store.get(args.task_id)
    if task is None:
        print(f"error: no such task {args.task_id}", file=sys.stderr)
        return 1
    print(f"task {task.id}")
    print(f"  target:                 {task.target}")
    print(f"  purpose:                {task.purpose}")
    print(f"  cadence:                every {task.cadence_seconds}s")
    print(f"  allowed actions:        read-only")
    print(f"  notification conditions:{task.notification_conditions}")
    print(f"  stop conditions:        {task.stop_conditions}")
    print(f"  state:                  {task.state.value}")
    if task.blocker:
        print(f"  blocker:                {task.blocker}")
    entries = activity.query(task_id=task.id, limit=5)
    if entries:
        print("  recent activity:")
        for entry in reversed(entries):
            print(f"    {_fmt_time(entry.at)}  {entry.kind}: {entry.message}")
    return 0


# -- activity / inbox -----------------------------------------------------------


def _run_activity(args: argparse.Namespace) -> int:
    _, store, activity, _, _, _ = _wiring()
    entries = activity.query(task_id=getattr(args, "task_id", None), limit=50)
    if not entries:
        print("no activity yet")
        return 0
    for entry in reversed(entries):
        print(f"{_fmt_time(entry.at)}  {entry.kind:<14} {entry.message}")
    return 0


def _run_inbox(_: argparse.Namespace) -> int:
    from nanodot.native.notifier import NativeNotifier

    sink = NativeNotifier(os_notify=False)
    entries = sink.list()
    if not entries:
        print("inbox is empty")
        return 0
    for entry in reversed(entries):
        print(f"{_fmt_time(entry.at)}  {entry.message}  ({entry.evidence.get('url', '')})")
    return 0


# -- runner ------------------------------------------------------------------------


def _pidfile() -> Path:
    return data_home() / "runner.pid"


def _run_runner(args: argparse.Namespace) -> int:
    import threading

    from nanodot.native.daemon import RunnerDaemon

    _, store, _, _, _, loop = _wiring()
    daemon = RunnerDaemon(loop, store)
    if args.once:
        attempted = daemon.tick()
        blockers = [t for t in store.list() if t.blocker]
        if blockers:
            for task in blockers:
                print(f"blocked: {task.id} ({task.target}): {task.blocker}",
                      file=sys.stderr)
            return 1
        print(f"ran {attempted} task(s)")
        return 0
    stop = threading.Event()

    def _sigint(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)
    print("nanodot runner started — Ctrl-C to stop", flush=True)
    daemon.serve(stop)
    print("nanodot runner stopped")
    return 0


def _run_start(_: argparse.Namespace) -> int:
    if _runner_alive():
        print("runner is already running")
        return 0
    log = open(data_home() / "runner.log", "ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "nanodot.cli", "runner"],
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    _pidfile().write_text(str(process.pid))
    print(f"runner started (pid {process.pid}) — logs: {data_home() / 'runner.log'}")
    return 0


def _runner_alive() -> bool:
    pid_file = _pidfile()
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _run_stop(_: argparse.Namespace) -> int:
    pid_file = _pidfile()
    if not pid_file.exists():
        print("runner is not running")
        return 0
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    pid_file.unlink()
    print("runner stopped")
    return 0


def _run_status(_: argparse.Namespace) -> int:
    if _runner_alive():
        pid = _pidfile().read_text().strip()
        print(f"runner is running (pid {pid})")
        return 0
    print("runner is not running")
    return 1


# -- entrypoint ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "config":
        return _run_config(args)
    if args.command == "watch":
        return _run_watch(args)
    if args.command == "activity":
        return _run_activity(args)
    if args.command == "inbox":
        return _run_inbox(args)
    if args.command == "runner":
        return _run_runner(args)
    if args.command == "start":
        return _run_start(args)
    if args.command == "stop":
        return _run_stop(args)
    if args.command == "status":
        return _run_status(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

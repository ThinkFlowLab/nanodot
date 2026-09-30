"""Command-line interface. CLI/TUI is the first interface per the issue #1 review;
a local web UI can layer on the same core later."""

from __future__ import annotations

import argparse
import sys

from nanodot import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanodot",
        description="A minimal, local-first AI assistant with persistent memory "
        "and proactive task execution.",
    )
    parser.add_argument("--version", action="version", version=f"nanodot {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    config = subparsers.add_parser("config", help="view or set configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_set = config_sub.add_parser("set", help="set a value")
    config_set.add_argument("name")
    config_set.add_argument("value")
    config_unset = config_sub.add_parser("unset", help="remove a value")
    config_unset.add_argument("name")
    config_sub.add_parser("list", help="list configured values (secrets masked)")

    return parser


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "config":
        return _run_config(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

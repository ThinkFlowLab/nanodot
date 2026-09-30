"""Command-line interface. CLI/TUI is the first interface per the issue #1 review;
a local web UI can layer on the same core later."""

from __future__ import annotations

import argparse
import getpass
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
    config_set.add_argument("value", nargs="?", help="value; use - for stdin, "
                            "or omit a secret value for a hidden prompt")
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
        value = args.value
        if value == "-":
            value = sys.stdin.readline().rstrip("\r\n")
        elif value is None and is_secret_name(args.name):
            if not sys.stdin.isatty():
                print("error: use - to read a secret from stdin", file=sys.stderr)
                return 1
            value = getpass.getpass(f"{args.name}: ")
        if value is None or (is_secret_name(args.name) and not value):
            print("error: a nonempty value is required", file=sys.stderr)
            return 1
        if is_secret_name(args.name):
            store.set(args.name, value)
            config.unset(args.name)  # remove a legacy plaintext copy after safe save
        else:
            try:
                config.set(args.name, value)
            except ValueError as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
    elif args.config_command == "unset":
        if is_secret_name(args.name):
            store.unset(args.name)
            config.unset(args.name)
        else:
            config.unset(args.name)
    else:  # list
        rows = [(key, MASK if is_secret_name(key) else str(config.get(key)))
                for key in config.keys() if key not in store.names()]
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

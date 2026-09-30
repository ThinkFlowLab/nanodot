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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Local data directory convention.

Everything nanodot persists lives under one home directory (default
``~/.nanodot``, overridable via ``NANODOT_HOME`` for tests and alternate
installs). Nothing may be written outside it.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_OVERRIDE = "NANODOT_HOME"


def data_home() -> Path:
    """Return the nanodot data directory, creating it on first use."""
    override = os.environ.get(ENV_OVERRIDE)
    base = Path(override).expanduser() if override else Path.home() / ".nanodot"
    base.mkdir(parents=True, exist_ok=True)
    return base


def database_path() -> Path:
    """Path of the single inspectable SQLite database."""
    return data_home() / "nanodot.db"

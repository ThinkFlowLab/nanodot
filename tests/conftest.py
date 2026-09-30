"""Shared fixtures. Every test runs against an isolated NANODOT_HOME."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "nanodot-home"
    monkeypatch.setenv("NANODOT_HOME", str(home_dir))
    return home_dir

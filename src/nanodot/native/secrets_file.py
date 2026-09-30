"""File-backed secret store.

A plain JSON file with 0600 permissions inside the data home. Chosen over
the OS keychain to stay dependency-free, portable, and inspectable with
standard tools; the data home is local-first by design. Raw secret values
exist nowhere else on disk (enforced by tests).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nanodot.paths import data_home

SECRETS_FILE = "secrets.json"


class FileSecretStore:
    def __init__(self, base: Path | None = None) -> None:
        self._path = (base or data_home()) / SECRETS_FILE

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _save(self, values: dict[str, str]) -> None:
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(values, fh, indent=2)
        os.chmod(self._path, 0o600)

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        values = self._load()
        values[name] = value
        self._save(values)

    def unset(self, name: str) -> None:
        values = self._load()
        values.pop(name, None)
        self._save(values)

    def names(self) -> list[str]:
        return sorted(self._load())

"""Plain (non-secret) configuration, persisted as config.json in the data home.

Secret-named keys (see redaction.is_secret_name) are routed to the secret
store by the CLI; this file never contains secret material.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanodot.paths import data_home

CONFIG_FILE = "config.json"


class Config:
    def __init__(self, base: Path | None = None) -> None:
        self._path = (base or data_home()) / CONFIG_FILE

    def get(self, key: str, default: object = None) -> object:
        if not self._path.exists():
            return default
        return json.loads(self._path.read_text()).get(key, default)

    def set(self, key: str, value: object) -> None:
        values: dict[str, object] = {}
        if self._path.exists():
            values = json.loads(self._path.read_text())
        values[key] = value
        self._path.write_text(json.dumps(values, indent=2))

    def unset(self, key: str) -> None:
        if self._path.exists():
            values = json.loads(self._path.read_text())
            values.pop(key, None)
            self._path.write_text(json.dumps(values, indent=2))

    def keys(self) -> list[str]:
        if not self._path.exists():
            return []
        return sorted(json.loads(self._path.read_text()))

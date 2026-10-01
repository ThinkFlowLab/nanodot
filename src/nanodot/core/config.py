"""Plain (non-secret) configuration, persisted as config.json in the data home.

Secret-named keys (see redaction.is_secret_name) are routed to the secret
store by the CLI; this file never contains secret material.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanodot.paths import data_home

CONFIG_FILE = "config.json"


def _validated_value(key: str, value: object) -> object:
    if key == "github-auth-mode" and value not in ("token", "anonymous"):
        raise ValueError("github-auth-mode must be token or anonymous")
    if key == "os-notifications":
        if type(value) is bool:
            return value
        # Normalize legacy CLI strings as well as newly entered values.
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError("os-notifications must be true or false")
    return value


class Config:
    def __init__(self, base: Path | None = None) -> None:
        self._path = (base or data_home()) / CONFIG_FILE

    def get(self, key: str, default: object = None) -> object:
        if not self._path.exists():
            return default
        values = json.loads(self._path.read_text())
        if not isinstance(values, dict):
            raise ValueError("config.json must contain a JSON object")
        if key not in values:
            return default
        return _validated_value(key, values[key])

    def set(self, key: str, value: object) -> None:
        if key == "mode" and value != "readonly":
            raise ValueError("only readonly mode is available; gated/auto modes are not implemented")
        value = _validated_value(key, value)
        values: dict[str, object] = {}
        if self._path.exists():
            values = json.loads(self._path.read_text())
        if not isinstance(values, dict):
            raise ValueError("config.json must contain a JSON object")
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

"""Plain (non-secret) configuration, persisted as config.json in the data home.

Secret-named keys (see redaction.is_secret_name) are routed to the secret
store by the CLI; this file never contains secret material.

Writes serialize through a sidecar flock and replace the file atomically, so
concurrent CLI invocations can neither interleave read-modify-write cycles
(silently reverting a key) nor expose a truncated file to readers.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from nanodot.paths import data_home

CONFIG_FILE = "config.json"


MODE_KEYS = ("permission-mode", "mode")  # canonical name first; mode is legacy


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


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Cross-process mutual exclusion for read-modify-write cycles.

    The lock file is never unlinked: an unlinked lock would let two
    processes hold two different "locks" on the same path.
    """
    with open(path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class Config:
    def __init__(self, base: Path | None = None) -> None:
        self._path = (base or data_home()) / CONFIG_FILE
        self._lock = self._path.parent / f"{self._path.name}.lock"

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        values = json.loads(self._path.read_text())
        if not isinstance(values, dict):
            raise ValueError("config.json must contain a JSON object")
        return values

    def get(self, key: str, default: object = None) -> object:
        values = self._read()
        if key not in values:
            return default
        return _validated_value(key, values[key])

    def set(self, key: str, value: object) -> None:
        # Mode keys validate at set time only: a hand-edited file fails
        # closed at load (PermissionCenter.mode), never crashes the runner.
        if key in MODE_KEYS and value not in ("readonly", "gated", "auto"):
            raise ValueError(f"{key} must be readonly, gated, or auto")
        value = _validated_value(key, value)
        with _exclusive_lock(self._lock):
            values = self._read()
            values[key] = value
            _atomic_write(self._path, json.dumps(values, indent=2))

    def unset(self, key: str) -> None:
        with _exclusive_lock(self._lock):
            values = self._read()
            if key not in values:
                return  # nothing to remove; never materialize an empty file
            values.pop(key)
            _atomic_write(self._path, json.dumps(values, indent=2))

    def keys(self) -> list[str]:
        return sorted(self._read())

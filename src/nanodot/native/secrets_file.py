"""File-backed secret store.

A plain JSON file with 0600 permissions inside the data home. Chosen over
the OS keychain to stay dependency-free, portable, and inspectable with
standard tools; the data home is local-first by design. Atomic replacement
uses a private temporary sibling which is cleaned on handled failures; an
abrupt process kill can leave that private file behind.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from nanodot.paths import data_home

SECRETS_FILE = "secrets.json"


class FileSecretStore:
    def __init__(self, base: Path | None = None) -> None:
        self._path = (base or data_home()) / SECRETS_FILE

    def _load(self) -> dict[str, str]:
        try:
            expected = self._path.lstat()
        except FileNotFoundError:
            return {}
        if not stat.S_ISREG(expected.st_mode):
            raise ValueError("secret store must be a regular file, not a symlink")
        # Do not follow a link planted between lstat and open. Comparing
        # file identities also protects platforms without O_NOFOLLOW.
        fd = os.open(
            self._path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(fd, "r") as fh:
            actual = os.fstat(fh.fileno())
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise OSError("secret store changed while being opened")
            return json.load(fh)

    def _save(self, values: dict[str, str]) -> None:
        # Write a private sibling, then replace the directory entry. Never
        # truncate the live store or write through a planted destination link.
        # No long-lived cache: token rotations must be visible to the runner.
        fd, temporary = tempfile.mkstemp(
            prefix=".secrets-", suffix=".tmp", dir=self._path.parent,
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(values, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self._path)
            # Persist the rename as well as the contents on POSIX hosts.
            if os.name == "posix":
                directory = os.open(
                    self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

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

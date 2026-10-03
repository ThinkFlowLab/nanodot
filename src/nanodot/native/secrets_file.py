"""File-backed secret store.

A plain JSON file with 0600 permissions inside the data home. Chosen over
the OS keychain to stay dependency-free, portable, and inspectable with
standard tools; the data home is local-first by design. Atomic replacement
uses a private temporary sibling which is cleaned on handled failures; an
abrupt process kill can leave that private file behind.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from nanodot.paths import data_home

SECRETS_FILE = "secrets.json"


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Cross-process mutual exclusion for read-modify-write rotations.

    The lock file is never unlinked, matching the config store's contract.
    """
    with open(path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileSecretStore:
    def __init__(self, base: Path | None = None) -> None:
        self._path = (base or data_home()) / SECRETS_FILE
        self._lock = self._path.parent / f"{self._path.name}.lock"

    def _load(self) -> dict[str, str]:
        # A concurrent rotation atomically replaces the regular directory
        # entry between lstat and open, making the identity check fail. Both
        # the old and the new regular file hold safe complete contents: when
        # the entry still points at the regular file already opened, read it.
        # O_NOFOLLOW and the regular-file checks still reject a link or
        # special file planted in that window.
        for _ in range(3):
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
                if not stat.S_ISREG(actual.st_mode):
                    raise OSError("secret store is not a regular file")
                if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                    return json.load(fh)
                try:
                    observed = self._path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(observed.st_mode):
                    # The entry stopped being a regular file while it was
                    # being opened: tampering, not a rotation.
                    raise OSError("secret store changed while being opened")
                if (observed.st_dev, observed.st_ino) == (actual.st_dev, actual.st_ino):
                    return json.load(fh)
                # Still churning; retry the whole inspection pair.
        raise OSError("secret store kept changing while being read")

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
        with _exclusive_lock(self._lock):
            values = self._load()
            values[name] = value
            self._save(values)

    def unset(self, name: str) -> None:
        with _exclusive_lock(self._lock):
            values = self._load()
            values.pop(name, None)
            self._save(values)

    def names(self) -> list[str]:
        return sorted(self._load())

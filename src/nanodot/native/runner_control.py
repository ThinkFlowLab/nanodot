"""Local runner ownership and cooperative shutdown for Linux and macOS.

A lifetime flock, rather than a PID, is the authority for singleton ownership.
PIDs are display-only: shutdown uses a random, per-lease token in a local
request file. An old or recycled PID can therefore never signal another
process. Lock files must never be unlinked, even when the runner is stopped.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

_POLL_SECONDS = 0.05


class RunnerControlError(RuntimeError):
    """Runner state cannot be safely established or a deadline expired."""


class RunnerAlreadyRunning(RunnerControlError):
    """Another process owns the runner's lifetime lock."""


def _lock_path(pidfile: Path) -> Path:
    return pidfile.with_suffix(".lock")


def _stop_path(pidfile: Path) -> Path:
    return pidfile.with_suffix(".stop")


def _open_lock(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a+b")


def _try_lock(handle: BinaryIO) -> bool:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _read_record(path: Path) -> dict | None:
    try:
        with path.open() as handle:
            text = handle.read(4097)
        if len(text) > 4096:
            return None
        value = json.loads(text)
    except (FileNotFoundError, ValueError, UnicodeError):
        return None
    if not isinstance(value, dict):
        return None
    pid, token = value.get("pid"), value.get("token")
    if type(pid) is not int or pid <= 1:
        return None
    if not isinstance(token, str) or len(token) != 64:
        return None
    if any(char not in "0123456789abcdef" for char in token):
        return None
    return {"pid": pid, "token": token}


def _write_record(path: Path, record: dict) -> None:
    """Publish a complete, user-private record without partial-read races."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def startup_lock(pidfile: Path, timeout: float = 5.0) -> Iterator[None]:
    """Serialize background start/stop commands through their readiness wait."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _open_lock(pidfile.with_suffix(".start.lock")) as handle:
        while not _try_lock(handle):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerControlError("another start/stop command is still in progress")
            time.sleep(min(_POLL_SECONDS, remaining))
        yield


@contextmanager
def configuration_lock(pidfile: Path) -> Iterator[None]:
    """Hold runner policy stable against both background and foreground starts.

    Configuration changes never publish a runner record. The lifetime lock
    stays held through the write, closing the gap between status and mutation.
    """
    with startup_lock(pidfile):
        with _open_lock(_lock_path(pidfile)) as handle:
            if not _try_lock(handle):
                raise RunnerAlreadyRunning("runner is already running")
            yield


class RunnerLease:
    """Hold exclusive runner ownership until the scheduler has fully stopped.

    Use as a context manager around the entire runner, including one-shot
    passes. ``stop`` is the same Event passed to ``RunnerDaemon.serve``.
    """

    def __init__(
        self, pidfile: Path, stop: threading.Event,
        *, prepare: Callable[[], None] | None = None,
    ) -> None:
        self.pidfile = pidfile
        self.pid = os.getpid()
        self._record = {"pid": self.pid, "token": secrets.token_hex(32)}
        self._stop = stop
        self._finished = threading.Event()
        self._handle: BinaryIO | None = None
        self._thread: threading.Thread | None = None
        self._prepare = prepare

    def __enter__(self) -> RunnerLease:
        self._handle = _open_lock(_lock_path(self.pidfile))
        try:
            acquired = _try_lock(self._handle)
        except BaseException:
            self._handle.close()
            self._handle = None
            raise
        if not acquired:
            self._handle.close()
            self._handle = None
            raise RunnerAlreadyRunning("runner is already running")
        try:
            # Ownership proves these files cannot belong to a live runner.
            # Remove stale readiness before preparation can block or fail.
            self.pidfile.unlink(missing_ok=True)
            _stop_path(self.pidfile).unlink(missing_ok=True)
            # Load configuration while owning the lifetime lock, before
            # publishing readiness. A failed setup must never look started.
            if self._prepare is not None:
                self._prepare()
            _write_record(self.pidfile, self._record)
            self._thread = threading.Thread(
                target=self._watch_stop, name="nanodot-runner-control", daemon=True,
            )
            self._thread.start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _watch_stop(self) -> None:
        while not self._finished.wait(_POLL_SECONDS):
            try:
                requested = _read_record(_stop_path(self.pidfile))
            except OSError:
                # An unreadable request is not authorization to stop.
                continue
            if requested == self._record:
                self._stop.set()
                return

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._finished.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()
        try:
            for path in (self.pidfile, _stop_path(self.pidfile)):
                if _read_record(path) == self._record:
                    path.unlink(missing_ok=True)
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


def running_pid(pidfile: Path) -> int | None:
    """Return the live lease owner's PID, never trusting a pidfile alone.

    A held lock with missing/corrupt metadata is an error, not evidence that
    it is safe to start another daemon or send a signal to the saved PID.
    """
    with _open_lock(_lock_path(pidfile)) as handle:
        if _try_lock(handle):
            return None
        record = _read_record(pidfile)
        if record is None:
            raise RunnerControlError("runner is starting or its metadata is unavailable")
        # It may have finished while we read its metadata.
        if _try_lock(handle):
            return None
        return record["pid"]


def stop_runner(pidfile: Path, timeout: float = 5.0) -> bool:
    """Request a cooperative stop and wait for actual lifetime-lock release.

    Return False if no runner owns the lock; True after the targeted runner
    releases it. A replacement owner is never targeted by this request.
    A timeout retains metadata and the pending stop request, and raises.
    This function deliberately never sends an OS signal to a stored PID.
    """
    timeout = max(0.0, timeout)
    with startup_lock(pidfile, timeout=timeout):
        # Start the cooperative-stop window only once startup serialization
        # is done: time spent queued behind another start/stop command must
        # not be deducted from the runner's actual stop budget.
        deadline = time.monotonic() + timeout
        with _open_lock(_lock_path(pidfile)) as handle:
            if _try_lock(handle):
                pidfile.unlink(missing_ok=True)
                _stop_path(pidfile).unlink(missing_ok=True)
                return False
            record = _read_record(pidfile)
            if record is None:
                raise RunnerControlError(
                    "runner holds its lock but its identity is unavailable; retry stop"
                )
            _write_record(_stop_path(pidfile), record)
            while True:
                if _try_lock(handle):
                    # We own the free lock, so no new runner can publish yet.
                    pidfile.unlink(missing_ok=True)
                    _stop_path(pidfile).unlink(missing_ok=True)
                    return True
                current = _read_record(pidfile)
                if current is not None and current != record:
                    # Metadata alone cannot prove shutdown. It may have been
                    # edited, or a foreground replacement may now own the lock.
                    raise RunnerControlError(
                        "runner identity changed before shutdown completed; "
                        "refusing to target a different runner"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunnerControlError(
                        f"runner did not stop within {timeout:g}s; stop is still pending"
                    )
                time.sleep(min(_POLL_SECONDS, remaining))

"""Shared fixtures. Every test runs against an isolated NANODOT_HOME."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly before accidental real network I/O, including raw sockets.

    HTTP adapter tests must inject fake transports. Dead proxy variables are
    inherited by CLI subprocesses; the socket/DNS guard applies in pytest.
    """
    def blocked(*args, **kwargs):
        raise AssertionError("network access is disabled in tests; use a fake port")

    for name in (
        "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
        "gethostbyaddr", "getnameinfo",
    ):
        monkeypatch.setattr(socket, name, blocked)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, blocked)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
        monkeypatch.setenv(name.lower(), "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "nanodot-home"
    monkeypatch.setenv("NANODOT_HOME", str(home_dir))
    return home_dir

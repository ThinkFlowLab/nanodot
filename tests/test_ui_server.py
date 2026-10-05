"""Loopback UI server acceptance tests (#110 skeleton).

Handler-level only: no real sockets, per the offline tripwire.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from nanodot import __version__
from nanodot.ui.server import LOOPBACK, UIHandler, UIServer, handler_exchange

SRC = Path(__file__).resolve().parent.parent / "src" / "nanodot"


def _get(path: str) -> tuple[int, bytes]:
    return handler_exchange(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())


def test_shell_serves_the_placeholder_screens() -> None:
    status, body = _get("/")
    assert status == 200
    assert __version__.encode() in body
    for screen in (b"Tasks", b"Inbox", b"Activity", b"Memory", b"Approvals"):
        assert screen in body


def test_health_reports_the_version() -> None:
    status, body = _get("/api/health")
    assert status == 200
    assert json.loads(body) == {"ok": True, "version": __version__}


def test_everything_else_is_a_404_including_traversal_shapes() -> None:
    for path in ("/nope", "/../etc/passwd", "/%2e%2e/etc/passwd", "/api"):
        status, _ = _get(path)
        assert status == 404, path


def test_the_only_bind_site_is_loopback() -> None:
    import inspect

    assert LOOPBACK == "127.0.0.1"
    source = inspect.getsource(UIServer.__init__)
    assert "LOOPBACK" in source  # the bind site names the loopback constant
    # And no other construction site exists in the package.
    for py in (SRC / "ui").rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "UIServer"
            ):
                assert py.name == "server.py", f"UIServer built in {py.name}"


def test_http_verbs_other_than_get_are_not_routed() -> None:
    # The skeleton is read-only: no POST/PUT/DELETE handlers exist.
    for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
        assert not hasattr(UIHandler, verb), verb

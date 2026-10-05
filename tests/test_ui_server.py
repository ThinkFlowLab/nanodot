"""Loopback UI acceptance tests (RFC #100 P0 / #110).

Two tiers, both socket-free: the pure request core (``NanodotUI.handle``)
carries every security and routing row, and ``handler_exchange`` drives
the real http.server adapter with raw HTTP bytes. The CLI-carrying rows
run the REAL CLI in child processes against an isolated NANODOT_HOME —
the UI's only data path. One final smoke exercises real loopback sockets
in a child process, outside pytest's in-process socket guard.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nanodot import __version__
from nanodot.core.activity import ActivityLog
from nanodot.core.permissions import PermissionCenter
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ui.server import (
    LOOPBACK,
    NanodotUI,
    UIHandler,
    UIServer,
    handler_exchange,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "nanodot"
TARGET = PRTarget.parse("thinkflowlab/nanodot#7")
SECRET = "ghp_super_secret_token_value_42"
PORT = 8765


def _seed(home: Path) -> str:
    store = TaskStore(path=home / "nanodot.db")
    task = store.create(
        Task(target=TARGET, purpose="watch", cadence_seconds=300, next_check_at=0.0)
    )
    ActivityLog(path=home / "nanodot.db").append(
        task.id, "checks-failed", "ci failing on s1", at=1000.0
    )
    store.close()
    return task.id


def _ui(**kwargs) -> NanodotUI:
    return NanodotUI(port=PORT, **kwargs)


def _ok(ui: NanodotUI) -> dict[str, str]:
    return {"Host": f"127.0.0.1:{PORT}", "X-UI-Token": ui.token}


def _get(ui: NanodotUI, path: str, headers: dict | None = None):
    return ui.handle("GET", path, headers if headers is not None else _ok(ui))


# -- the page and the token gate -------------------------------------------


def test_the_page_lives_only_at_the_exact_token_path(home: Path) -> None:
    ui = _ui()
    status, _, body = _get(ui, ui.page_path)
    assert status == 200 and b"<html" in body
    # Screens and version render; the page carries the token for its own
    # API calls — nowhere else.
    for screen in (b"tasks", b"inbox", b"memory", b"approvals"):
        assert screen in body
    assert __version__.encode() in body
    assert ui.token.encode() in body

    assert _get(ui, "/")[0] == 404
    assert _get(ui, f"/{ui.token}x/")[0] == 404
    status, _, _ = _get(
        ui, "/api/tasks", headers={"Host": "evil.example:8765", "X-UI-Token": ui.token}
    )
    assert status == 403  # DNS-rebinding guard


def test_every_api_call_needs_the_per_boot_token(home: Path) -> None:
    ui = _ui()
    status, _, body = _get(ui, "/api/tasks", headers={"Host": "127.0.0.1:8765"})
    assert status == 403 and b"token" in body
    status, _, _ = _get(
        ui, "/api/health", headers={"Host": "127.0.0.1:8765", "X-UI-Token": "wrong"}
    )
    assert status == 403


def test_health_reports_the_version_behind_the_token(home: Path) -> None:
    ui = _ui()
    status, ctype, body = _get(ui, "/api/health")
    assert status == 200 and ctype == "application/json"
    assert json.loads(body) == {"ok": True, "version": __version__}


# -- routing: reads are GET, mutations are POST, nothing else exists -------


def test_unknown_routes_traversal_shapes_and_wrong_methods_are_refused(
    home: Path,
) -> None:
    ui = _ui()
    for path in ("/nope", "/../etc/passwd", "/%2e%2e/etc/passwd", "/api", "/api/"):
        assert _get(ui, path)[0] == 404, path
    assert _get(ui, "/api/task/bad!id")[0] == 400  # ids stay path-shaped
    status, _, _ = ui.handle("POST", "/api/tasks", _ok(ui))
    assert status == 405  # reads are GET-only
    status, _, _ = ui.handle("GET", "/api/runner/stop", _ok(ui))
    assert status == 405  # mutations are POST-only
    status, _, _ = ui.handle("POST", "/api/watch/add", _ok(ui))
    assert status == 404  # the POST allowlist is closed
    # The verbs that exist are exactly GET and POST.
    for verb in ("do_PUT", "do_DELETE", "do_PATCH"):
        assert not hasattr(UIHandler, verb), verb


def test_the_only_bind_site_is_loopback() -> None:
    import inspect

    assert LOOPBACK == "127.0.0.1"
    source = inspect.getsource(UIServer.__init__)
    assert "LOOPBACK" in source  # the bind site names the loopback constant
    for py in (SRC / "ui").rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "UIServer"
            ):
                assert py.name == "server.py", f"UIServer built in {py.name}"


# -- the real CLI data path --------------------------------------------------


def test_reads_carry_real_cli_output(home: Path) -> None:
    task_id = _seed(home)
    ui = _ui()

    status, ctype, body = _get(ui, "/api/tasks")
    assert status == 200 and ctype == "application/json"
    payload = json.loads(body)
    assert payload["exit"] == 0 and TARGET.owner in payload["stdout"]

    status, _, body = _get(ui, f"/api/activity/{task_id}")
    assert json.loads(body)["exit"] == 0
    assert "checks-failed" in json.loads(body)["stdout"]


def test_a_mutation_runs_the_real_cli_and_changes_real_state(home: Path) -> None:
    _seed(home)
    center = PermissionCenter(path=home / "nanodot.db")
    request = center.request(
        action="comment", target=str(TARGET), scope="watch:abc",
        task_id="t1", content={"body": "hello"},
    )
    center.close()

    ui = _ui()
    status, _, body = ui.handle("POST", f"/api/deny/{request.id}", _ok(ui))
    assert status == 200
    payload = json.loads(body)
    assert payload["exit"] == 0 and request.id in payload["stdout"]

    center = PermissionCenter(path=home / "nanodot.db")
    assert center.request_history()[0].state == "denied"
    center.close()


def test_no_secret_ever_reaches_the_served_bytes(home: Path) -> None:
    _seed(home)
    FileSecretStore().set("github-token", SECRET)
    ui = _ui()
    for path in ("/api/tasks", "/api/inbox", "/api/memory", "/api/approvals",
                 "/api/status", "/api/health"):
        _, _, body = _get(ui, path)
        assert SECRET.encode() not in body, path


def test_runner_status_reflects_the_real_runner(home: Path) -> None:
    _seed(home)
    ui = _ui()
    _, _, body = _get(ui, "/api/status")
    assert json.loads(body)["exit"] == 1  # no runner in the isolated home


# -- the http.server adapter, still without sockets --------------------------


def test_the_adapter_serves_the_page_and_enforces_the_core() -> None:
    ui = _ui()
    raw = (
        f"GET {ui.page_path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n\r\n"
    ).encode()
    status, body = handler_exchange(raw, ui)
    assert status == 200 and b"<html" in body

    status, _ = handler_exchange(
        f"GET /api/tasks HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n\r\n".encode(), ui
    )
    assert status == 403  # no token, through the real parse path


# -- one real-socket smoke, run as a child outside the socket guard ---------


def test_loopback_smoke_over_real_sockets(home: Path) -> None:
    """The http.server glue, driven by a child process (pytest's socket
    guard is in-process only; the child has no patches and never leaves
    loopback)."""
    script = """
import json, sys, threading, urllib.error, urllib.request
sys.path.insert(0, {src!r})
from nanodot.ui.server import NanodotUI, UIServer

ui = NanodotUI(port=8799)
server = UIServer(ui, port=8799)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

# The UI is loopback-only and never proxied; the client is too.
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))

def fetch(path, method="GET", headers=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8799" + path, method=method,
        headers=headers or {{}}, data=b"" if method == "POST" else None)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()

status, body = fetch(ui.page_path, headers={{"Host": "127.0.0.1:8799"}})
assert status == 200 and b"<html" in body, "page"
status, body = fetch("/api/health", headers={{
    "Host": "127.0.0.1:8799", "X-UI-Token": ui.token}})
assert status == 200 and json.loads(body)["ok"], "health"
status, _ = fetch("/api/health", headers={{
    "Host": "127.0.0.1:8799", "X-UI-Token": "wrong"}})
assert status == 403, "token gate over sockets"
server.shutdown()
print("SMOKE-OK")
""".format(src=str(SRC.parent))
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "SMOKE-OK" in result.stdout

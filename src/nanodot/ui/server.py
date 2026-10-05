"""The stdlib loopback server behind `nanodot ui` (#100 Q1(a), #110).

http.server on 127.0.0.1 only. The handler serves the single-page shell
and a versioned health endpoint; everything else is 404 by default so
each screen endpoint lands as a deliberate addition. Handler-level code
is socket-free so the offline tripwire can exercise all of it.
"""

from __future__ import annotations

import html
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nanodot import __version__

LOOPBACK = "127.0.0.1"

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nanodot</title>
<style>
 body {{ font-family: ui-monospace, monospace; margin: 2rem; background: #111; color: #eee; }}
 h1 {{ font-size: 1.1rem; }} ul {{ list-style: none; padding: 0; }}
 li {{ padding: .3rem 0; }} .muted {{ color: #888; }}
</style>
</head>
<body>
<h1>nanodot {version}</h1>
<p class="muted">loopback UI — screens land in P0 (issue #110)</p>
<ul>
 <li>Tasks</li><li>Inbox</li><li>Activity</li><li>Memory</li><li>Approvals</li>
</ul>
</body>
</html>
"""


class UIHandler(BaseHTTPRequestHandler):
    """Routes: `/` (shell), `/api/health` (version JSON). Everything else
    404s — no filesystem serving, no traversal, no discovery."""

    server_version = f"nanodot-ui/{__version__}"

    def do_GET(self) -> None:  # noqa: N802  (http.server naming)
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = _SHELL.format(version=html.escape(__version__)).encode()
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/api/health":
            body = json.dumps(
                {"ok": True, "version": __version__}
            ).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def log_message(self, format: str, *args: object) -> None:
        # Quiet by default: the loopback UI is not an audit surface.
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class UIServer(ThreadingHTTPServer):
    """Bound to LOOPBACK only — the invariant is in the constructor."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 0) -> None:
        # The only bind site: loopback, ephemeral when port=0. A
        # non-loopback UI is never constructible from here.
        super().__init__((LOOPBACK, port), UIHandler)

    def port(self) -> int:
        return self.server_address[1]

    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port()}/"


def serve(port: int = 0, open_browser: bool = True) -> str:
    """Start the UI server (blocking; Ctrl-C to stop) and return its URL.
    Browser opening is skipped when asked or when headless."""
    server = UIServer(port=port)
    url = server.url()
    if open_browser:
        import webbrowser

        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # Ctrl-C: a UI is a foreground convenience, not a daemon
    finally:
        server.server_close()
    return url


def handler_exchange(raw_request: bytes) -> tuple[int, bytes]:
    """Drive one request through UIHandler without any socket — the
    offline-test entrypoint. Returns (status, body)."""
    handler = UIHandler.__new__(UIHandler)

    class _SilentServer:  # handler reads server attributes only
        def __getattr__(self, name: str) -> object:
            return None

    handler.server = _SilentServer()  # type: ignore[assignment]
    handler.rfile = io.BytesIO(raw_request)  # type: ignore[assignment]
    handler.wfile = io.BytesIO()  # type: ignore[assignment]
    handler.client_address = (LOOPBACK, 0)  # type: ignore[assignment]
    # Mirror handle_one_request's loop: readline feeds parse_request.
    handler.raw_requestline = handler.rfile.readline()  # type: ignore[assignment]
    handler.parse_request()
    handler.do_GET()
    handler.wfile.seek(0)
    raw = handler.wfile.getvalue()
    status = int(raw.split(b" ", 2)[1])
    body = raw.split(b"\r\n\r\n", 1)[1]
    return status, body

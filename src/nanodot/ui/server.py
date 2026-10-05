"""The stdlib loopback UI behind `nanodot ui` (RFC #100 / #110).

Skeleton landed in #114; screens, security rows, and runner lifecycle
landed on top of it. The design is two-tier on purpose:

- ``NanodotUI`` is a pure request core (method, path, headers, body in;
  status, content type, body out) with every security invariant inside
  it, so all of them are offline-testable without sockets.
- ``UIHandler`` / ``UIServer`` are a thin http.server adapter, and
  ``handler_exchange`` drives that adapter with raw HTTP bytes, also
  without sockets.

The UI is an unprivileged consumer, exactly like the DSH tools: every
read renders CLI output and every mutation spawns the CLI in a child
process — no second writer, no new egress, and approve()-is-CLI-only is
untouched by construction. The only bind site is loopback (the UIServer
constructor), so a non-loopback UI is not constructible.
"""

from __future__ import annotations

import io
import json
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from nanodot import __version__

LOOPBACK = "127.0.0.1"
PORT_ATTEMPTS = 10
CALL_TIMEOUT_SECONDS = 90

# The CLI bootstrap for child processes — the same shape bin/nanodot.cjs
# uses. No shell, no string interpolation: arguments travel as argv.
CLI_BOOTSTRAP = "import sys; from nanodot.cli import main; raise SystemExit(main())"

_FLAT_GET = {"status", "tasks", "inbox", "memory", "approvals"}
_PARAM_GET = {"task": "watch show", "activity": "activity"}
_PARAM_POST = {"approve": "approve", "deny": "deny"}
_ACTION_POST = {"runner/start": "start", "runner/stop": "stop"}


class UICallError(Exception):
    """The CLI child could not be started at all — typed, never prose."""


class _BadId(ValueError):
    pass


class NanodotUI:
    """The request core: no sockets, no server — fully offline-testable."""

    def __init__(
        self,
        runner: Callable[[list[str]], dict] | None = None,
        token: str | None = None,
        port: int = 0,
    ) -> None:
        # Per-boot secret: the page lives at /<token>/ and every API call
        # must echo it — a stray cross-site POST from another browser tab
        # has neither the path nor the header.
        self._token = token or secrets.token_urlsafe(16)
        self._port = port
        self._runner = runner or _subprocess_call

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        return self._port

    @property
    def page_path(self) -> str:
        return f"/{self._token}/"

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: bytes = b"",
    ) -> tuple[int, str, bytes]:
        """Route one request. Header names are matched case-insensitively."""
        lowered = {key.lower(): value for key, value in (headers or {}).items()}
        # DNS-rebinding guard: the conversation is with this loopback
        # server, so the host the browser names must be exactly it.
        if lowered.get("host", "") not in (
            f"{LOOPBACK}:{self._port}", f"localhost:{self._port}",
        ):
            return _reply(403, "text/plain; charset=utf-8",
                          b"this interface is loopback-only")

        if path in (self.page_path, f"/{self._token}"):
            if method != "GET":
                return _reply(405, "text/plain; charset=utf-8", b"method not allowed")
            return _reply(200, "text/html; charset=utf-8", _page(self._token))

        if not path.startswith("/api/"):
            return _reply(404, "text/plain; charset=utf-8", b"not found")
        if lowered.get("x-ui-token") != self._token:
            return _reply(403, "text/plain; charset=utf-8",
                          b"missing or wrong UI token")

        route = path[len("/api/"):].rstrip("/")
        parts = route.split("/") if route else []
        argv: list[str] | None = None
        wants_post = False
        try:
            if route == "health":
                return _reply(200, "application/json", json.dumps(
                    {"ok": True, "version": __version__}
                ).encode())
            if len(parts) == 1 and parts[0] in _FLAT_GET:
                argv = {"status": ["status"], "tasks": ["watch", "list"],
                        "inbox": ["inbox"], "memory": ["memory", "list"],
                        "approvals": ["approvals"]}[parts[0]]
            elif len(parts) == 2 and parts[0] in _PARAM_GET:
                _require_id(parts[1])
                argv = _PARAM_GET[parts[0]].split() + [parts[1]]
            elif len(parts) == 2 and parts[0] in _PARAM_POST:
                _require_id(parts[1])
                argv = ["approvals", _PARAM_POST[parts[0]], parts[1]]
                wants_post = True
            elif route in _ACTION_POST:
                argv = [_ACTION_POST[route]]
                wants_post = True
        except _BadId:
            return _reply(400, "text/plain; charset=utf-8", b"malformed id")
        if argv is None:
            return _reply(404, "text/plain; charset=utf-8", b"not found")
        if wants_post != (method == "POST"):
            return _reply(405, "text/plain; charset=utf-8", b"method not allowed")

        # The CLI's outcome is data: a nonzero exit is a rendered result
        # (the CLI already worded it), never an HTTP error.
        try:
            outcome = self._runner(argv)
        except UICallError as error:
            outcome = {"exit": 127, "stdout": "", "stderr": str(error)}
        return _reply(200, "application/json", json.dumps(outcome).encode())


def _require_id(task_id: str) -> None:
    """Path parameters stay path-shaped: one segment of plain id characters."""
    if not task_id or any(char not in "0123456789abcdefABCDEF-" for char in task_id):
        raise _BadId(task_id)


def _subprocess_call(argv: list[str]) -> dict:
    """Spawn the CLI as a child process and return its outcome as data.

    Arguments travel as argv entries (never a shell string); the child
    inherits the environment, so NANODOT_HOME selects the same data home
    the server was started with.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", CLI_BOOTSTRAP, *argv],
            capture_output=True, text=True, timeout=CALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UICallError(
            f"the CLI child failed to run: {error.__class__.__name__}"
        ) from error
    return {
        "exit": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _reply(status: int, content_type: str, body: bytes) -> tuple[int, str, bytes]:
    return status, content_type, body


class UIHandler(BaseHTTPRequestHandler):
    """Thin adapter over the request core. No filesystem serving, no
    traversal, no discovery: unknown paths are the core's 404s."""

    server_version = f"nanodot-ui/{__version__}"

    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, content_type, payload = self.server.ui.handle(  # type: ignore[attr-defined]
            method, self.path, dict(self.headers), body,
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 — http.server naming
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._serve("POST")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet by default: the loopback UI is not an audit surface, and
        # loopback personal data never lands in access logs.
        return


class UIServer(ThreadingHTTPServer):
    """Bound to LOOPBACK only — the invariant is in the constructor: a
    non-loopback UI is not constructible from here."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, ui: NanodotUI, port: int = 0) -> None:
        self.ui = ui
        super().__init__((LOOPBACK, port), UIHandler)

    def port(self) -> int:
        return self.server_address[1]

    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port()}{self.ui.page_path}"


def serve(port: int = 0, open_browser: bool = True) -> str:
    """Serve until Ctrl-C (a UI is a foreground convenience, not a
    daemon) and return the URL. Port 0 picks an ephemeral loopback port;
    an explicit port falls forward up to PORT_ATTEMPTS tries."""
    if port == 0:
        server = UIServer(NanodotUI(port=0), port=0)  # binds ephemeral
        # The core's Host guard must match the port the OS actually gave.
        server.ui = NanodotUI(port=server.port())
    else:
        server = None
        for candidate in range(port, port + PORT_ATTEMPTS):
            try:
                server = UIServer(NanodotUI(port=candidate), port=candidate)
                break
            except OSError:
                continue
        if server is None:
            raise OSError(
                f"no free loopback port in {port}..{port + PORT_ATTEMPTS - 1}"
            )
    url = server.url()
    print(f"nanodot ui — {url} (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("nanodot ui stopped", flush=True)
    return url


def handler_exchange(raw_request: bytes, ui: NanodotUI | None = None) -> tuple[int, bytes]:
    """Drive one request through UIHandler without any socket — the
    offline-test entrypoint for the adapter layer. Returns (status, body)."""
    handler = UIHandler.__new__(UIHandler)

    class _SilentServer:  # handler reads server attributes only
        def __getattr__(self, name: str) -> object:
            return None

    bound = ui or NanodotUI(runner=lambda argv: {"exit": 0, "stdout": "", "stderr": ""})

    class _Bound(_SilentServer):
        ui = bound

    handler.server = _Bound()  # type: ignore[assignment]
    handler.rfile = io.BytesIO(raw_request)  # type: ignore[assignment]
    handler.wfile = io.BytesIO()  # type: ignore[assignment]
    handler.client_address = (LOOPBACK, 0)  # type: ignore[assignment]
    # Mirror handle_one_request's loop: readline feeds parse_request.
    handler.raw_requestline = handler.rfile.readline()  # type: ignore[assignment]
    handler.parse_request()
    (handler.do_POST if raw_request.startswith(b"POST ") else handler.do_GET)()
    handler.wfile.seek(0)
    raw = handler.wfile.getvalue()
    status = int(raw.split(b" ", 2)[1])
    body = raw.split(b"\r\n\r\n", 1)[1]
    return status, body


def _page(token: str) -> bytes:
    return _PAGE_TEMPLATE.replace("__TOKEN__", token).replace(
        "__VERSION__", __version__,
    ).encode("utf-8")


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nanodot</title>
<style>
  :root{--bg:#0b1322;--panel:#0f1a2e;--node:#15233b;--line:rgba(148,163,184,.18);
        --ink:#e8eef8;--mut:#93a7c4;--acc:#38cfe8;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,
       BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:20px 24px;}
  header{display:flex;align-items:center;gap:14px;margin-bottom:16px;}
  h1{font-size:17px;font-weight:800;letter-spacing:.5px;}
  h1 span{color:var(--acc);}
  h1 small{color:var(--mut);font-weight:400;font-size:12px;margin-left:6px;}
  .dot{width:9px;height:9px;border-radius:50%;background:#64748b;}
  .dot.on{background:#34d399;box-shadow:0 0 8px rgba(52,211,153,.6);}
  .spacer{flex:1;}
  button{background:var(--node);color:var(--ink);border:1px solid var(--line);
         border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer;}
  button:hover{border-color:var(--acc);}
  nav{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;}
  nav button.active{border-color:var(--acc);color:var(--acc);}
  .badge{background:rgba(56,207,232,.15);color:var(--acc);border-radius:999px;
         padding:1px 8px;font-size:11px;font-weight:700;margin-left:6px;}
  pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;
      padding:16px;white-space:pre-wrap;word-break:break-word;font:12.5px/1.55
      ui-monospace,'JetBrains Mono',Menlo,monospace;color:var(--ink);
      max-height:72vh;overflow:auto;}
  .note{color:var(--mut);font-size:12px;margin:8px 2px;}
  .row{display:flex;gap:10px;align-items:center;}
  input{background:var(--node);border:1px solid var(--line);color:var(--ink);
        border-radius:8px;padding:6px 10px;font-size:13px;width:190px;}
</style>
</head>
<body>
<header>
  <h1>nano<span>dot</span><small>__VERSION__</small></h1>
  <div class="dot" id="dot" title="runner status"></div>
  <span class="note" id="runner-note">checking…</span>
  <div class="spacer"></div>
  <button id="btn-start">start runner</button>
  <button id="btn-stop">stop runner</button>
</header>
<nav id="tabs">
  <button data-tab="tasks" class="active">tasks</button>
  <button data-tab="inbox">inbox<span class="badge" id="inbox-badge" hidden>0</span></button>
  <button data-tab="activity">activity</button>
  <button data-tab="memory">memory</button>
  <button data-tab="approvals">approvals</button>
</nav>
<div class="row" id="id-row" hidden>
  <input id="task-id" placeholder="task id (from tasks)">
  <button id="btn-load">show</button>
  <span class="note">activity for one task</span>
</div>
<pre id="out">loading…</pre>
<p class="note">loopback-only · every action runs the nanodot CLI ·
nothing leaves this machine</p>
<script>
const TOKEN="__TOKEN__";
const api=(path,opts)=>fetch('/api/'+path,Object.assign({headers:{'X-UI-Token':TOKEN}},opts||{}));
let tab='tasks', lastInboxCount=null;
const seenKey='nanodot-inbox-seen';
async function call(path,opts){
  try{const r=await api(path,opts);const j=await r.json();
      return j.exit===0?(j.stdout||'(no output)'):'exit '+j.exit+'\\n'+(j.stderr||j.stdout);}
  catch(e){return 'request failed: '+e;}
}
async function refresh(){
  const out=document.getElementById('out');
  const idRow=document.getElementById('id-row');
  const tid=document.getElementById('task-id').value.trim();
  if(tab==='tasks'||tab==='inbox'||tab==='memory'||tab==='approvals'){
    idRow.hidden=true;
    out.textContent=await call(tab);
    if(tab==='inbox'){
      const text=out.textContent;
      const n=(text.match(/^20/gm)||[]).length;
      const seen=+(localStorage.getItem(seenKey)||0);
      const badge=document.getElementById('inbox-badge');
      if(lastInboxCount!==null&&n>seen&&n>0){badge.hidden=false;badge.textContent=n-seen;}
      else if(n===0){badge.hidden=true;}
      lastInboxCount=n;localStorage.setItem(seenKey,n);
    }
  } else if(tab==='activity'){
    idRow.hidden=false;
    out.textContent=tid?await call('activity/'+encodeURIComponent(tid))
      :'enter a task id above (tasks lists them)';
  }
}
async function runnerStatus(){
  let on=false;
  try{
    await api('health');
    const j=await api('status').then(r=>r.json());
    on=j.exit===0;
  }catch(e){}
  document.getElementById('dot').classList.toggle('on',on);
  document.getElementById('runner-note').textContent=
    on?'runner is running':'runner is not running';
}
document.getElementById('tabs').addEventListener('click',e=>{
  const b=e.target.closest('button[data-tab]');if(!b)return;
  tab=b.dataset.tab;
  document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('active',x===b));
  refresh();
});
document.getElementById('btn-load').addEventListener('click',refresh);
document.getElementById('task-id').addEventListener('keydown',e=>{if(e.key==='Enter')refresh();});
document.getElementById('btn-start').addEventListener('click',async()=>{
  document.getElementById('out').textContent=await call('runner/start',{method:'POST'});
  runnerStatus();});
document.getElementById('btn-stop').addEventListener('click',async()=>{
  document.getElementById('out').textContent=await call('runner/stop',{method:'POST'});
  runnerStatus();});
setInterval(()=>{refresh();runnerStatus();},5000);
refresh();runnerStatus();
</script>
</body>
</html>
"""

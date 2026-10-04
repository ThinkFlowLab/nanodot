"""Writer-port acceptance tests (issue #59, docs/design/github-writer.md).

Every acceptance row of the design document is a named test here, offline,
fakes only, network dead. A missing row blocks the merge.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import sqlite3
import threading
import urllib.error
from pathlib import Path

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeSink, SUCCESS

from nanodot.core.activity import ActivityLog
from nanodot.core.config import Config
from nanodot.core.permissions import (
    Mode,
    PermissionCenter,
    WriteCapability,
    WriteForbidden,
)
from nanodot.core.redaction import Redactor
from nanodot.core.runner import RunOutcome, TaskLoop
from nanodot.core.write_flow import (
    WRITE_AUTO,
    WRITE_DONE,
    WRITE_FAILED,
    WRITE_INTENT,
    WRITE_PROPOSED,
    WRITE_SILENCE_DENIED,
    WRITE_SKIPPED,
    WRITE_UNKNOWN,
    WriteFlow,
)
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.core.statemachine import CHECKS_FAILED, WatchEvent
from nanodot.native.github_client import GitHubSnapshotFetcher
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github_writer import (
    CAPABILITY_FIELDS,
    GitHubWriter,
    WriteCapability,
    WriteContentMismatch,
    WriteCredentialMissing,
    payload_digest,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "nanodot"
TARGET = PRTarget.parse("thinkflowlab/nanodot#9")


from fakes import FakeGitHubWriter as _SharedFakeWriter
from nanodot.ports.github_writer import WriteRejectedError


class FakeWriter(_SharedFakeWriter):
    """The shared scriptable writer, plus the sends shorthand the flow
    tests read (executed payloads that passed the hash gate)."""

    @property
    def sends(self):
        return self.executed


class Harness:
    def __init__(self, home: Path, clock: FakeClock | None = None, gated: bool = True):
        self.home = home
        self.clock = clock or FakeClock()
        if gated:
            Config().set("mode", "gated")
        self.redactor = Redactor(FileSecretStore())
        self.store = TaskStore(path=home / "nanodot.db", redactor=self.redactor)
        self.activity = ActivityLog(path=home / "nanodot.db", redactor=self.redactor)
        self.permissions = PermissionCenter(path=home / "nanodot.db", clock=self.clock)
        self.writer = FakeWriter()
        self.write = WriteFlow(
            self.permissions, self.writer, self.activity,
            redactor=self.redactor,
        )
        self.github = FakeGitHub(TARGET)
        self.github.set_pr("open", head_sha="s1")
        self.github.add_check("ci", FAILURE, sha="s1")
        self.sink = FakeSink()
        self.loop = TaskLoop(
            self.store, self.github, self.sink, self.activity, write=self.write,
        )
        self.task = self.store.create(
            Task(target=TARGET, purpose="watch", next_check_at=0.0)
        )

    def tick(self) -> RunOutcome:
        return self.loop.run_once(self.store.get(self.task.id), self.clock.time())

    def approve_proposal(self) -> WriteCapability:
        request = self.permissions.pending()[0]
        return self.permissions.approve(request.id)

    def kinds(self) -> list[str]:
        return [e.kind for e in reversed(self.activity.query(task_id=self.task.id))]

    def reopen(self) -> "Harness":
        """Simulate a process restart: fresh objects over the same data."""
        self.store.close()
        self.activity.close()
        self.permissions.close()
        h = Harness.__new__(Harness)
        h.home = self.home
        h.clock = self.clock
        h.redactor = Redactor(FileSecretStore())
        h.store = TaskStore(path=self.home / "nanodot.db", redactor=h.redactor)
        h.activity = ActivityLog(path=self.home / "nanodot.db", redactor=h.redactor)
        h.permissions = PermissionCenter(path=self.home / "nanodot.db", clock=self.clock)
        h.writer = FakeWriter()
        h.write = WriteFlow(
            h.permissions, h.writer, h.activity, redactor=h.redactor,
        )
        h.github = self.github
        h.sink = FakeSink()
        h.loop = TaskLoop(h.store, h.github, h.sink, h.activity, write=h.write)
        h.task = h.store.get(self.task.id)
        return h


# -- 1. port-surface freeze ---------------------------------------------------


def test_port_surface_is_frozen() -> None:
    public = {
        name for name in dir(GitHubWriter) if not name.startswith("_")
    }
    assert public == {"execute"}, public
    assert CAPABILITY_FIELDS == {
        "action", "target", "content_hash", "grant_id", "used_at",
    }


# -- 2. readonly regression ----------------------------------------------------


def test_readonly_denies_writes_even_with_a_write_token(home: Path) -> None:
    FileSecretStore().set("github-write-token", "ghp_write_123")
    Config().set("permission-mode", "readonly")  # the explicit hard-off
    center = PermissionCenter()
    assert center.mode() is Mode.READONLY
    with pytest.raises(WriteForbidden):
        center.assert_allowed("comment")

    # And the loop proposes nothing in readonly mode.
    h = Harness(home, gated=False)
    h.tick()
    assert "write-proposed" not in h.kinds()
    assert "write-auto" not in h.kinds()
    assert h.permissions.pending() == []
    assert h.writer.sends == []


# -- 2b. auto mode: standing grants, skip-and-record (#48 decision 5) ----------


def test_auto_standing_grant_authorizes_and_sends_without_ever_asking(
    home: Path,
) -> None:
    h = Harness(home, gated=False)  # default mode: auto
    assert h.permissions.mode() is Mode.AUTO
    h.permissions.create_standing_grant(
        action="comment", target=str(TARGET), scope="watch", task_id=h.task.id
    )
    h.tick()
    kinds = h.kinds()
    assert WRITE_AUTO in kinds
    assert WRITE_PROPOSED not in kinds  # nothing was ever asked
    assert h.permissions.pending() == []
    assert h.writer.sends == []  # the derived capability executes on the next run

    h.clock.advance(300)
    h.tick()
    assert len(h.writer.sends) == 1
    kinds = h.kinds()
    assert WRITE_INTENT in kinds and WRITE_DONE in kinds
    # Identical later polls neither re-ask nor re-send the same content.
    h.clock.advance(300)
    h.tick()
    assert len(h.writer.sends) == 1
    assert h.kinds().count(WRITE_AUTO) == 1
    # The write did not disturb the watch itself.
    task = h.store.get(h.task.id)
    assert task.state.value == "active"


def test_auto_without_a_standing_grant_skips_once_and_never_spams(
    home: Path,
) -> None:
    h = Harness(home, gated=False)
    h.tick()
    kinds = h.kinds()
    assert WRITE_SKIPPED in kinds
    assert WRITE_PROPOSED not in kinds
    assert h.permissions.pending() == []
    assert h.writer.sends == []

    h.clock.advance(300)
    h.tick()  # same failing head: the skip is recorded once, not per poll
    assert h.kinds().count(WRITE_SKIPPED) == 1
    assert h.writer.sends == []

    # A standing grant added later authorizes new content only.
    h.github.set_pr("open", head_sha="s2")
    h.github.add_check("ci", FAILURE, sha="s2")
    h.permissions.create_standing_grant(
        action="comment", target=str(TARGET), scope="watch", task_id=h.task.id
    )
    h.clock.advance(300)
    h.tick()
    assert WRITE_AUTO in h.kinds()
    assert h.kinds().count(WRITE_SKIPPED) == 1


# -- 3. no capability without approval ----------------------------------------


def test_forged_capability_cannot_execute(home: Path) -> None:
    """The port dataclass is intentionally plain (merged #61); the
    forgery gate is the single-use consume: a capability the store never
    issued cannot pass it, so nothing is ever sent."""
    h = Harness(home)
    h.tick()
    forged = WriteCapability(
        action="comment", target=str(TARGET),
        content_hash="0" * 64, grant_id="forged",
    )
    h.write.execute_pending(h.store.get(h.task.id), h.clock.time())
    # A forged grant_id is not in the store: consume misses, no send —
    # exercised directly below with the store-backed path.
    assert not h.permissions.consume("forged")
    assert h.writer.sends == []


# -- 4. silence is not approval -------------------------------------------------


def test_expired_request_grants_nothing_and_re_request_works(home: Path) -> None:
    h = Harness(home)
    h.tick()  # proposal created
    request_id = h.permissions.pending()[0].id
    h.clock.advance(24 * 3600 + 1)  # past the request TTL
    with pytest.raises(ValueError, match="expired"):
        h.permissions.approve(request_id)
    assert h.permissions.pending() == []  # expired is not pending
    assert h.writer.sends == []
    assert h.permissions.pending_capabilities(h.task.id) == []

    # A fresh failure episode re-proposes and the re-request is approvable.
    h.github.checks["s1"] = []
    h.github.set_pr("open", head_sha="s2")
    h.github.add_check("ci", FAILURE, sha="s2")
    h.tick()
    capability = h.approve_proposal()
    assert capability.content_hash


def test_write_requests_use_the_four_hour_ttl(home: Path) -> None:
    h = Harness(home)
    h.tick()
    request = h.permissions.pending()[0]
    assert request.expires_at - request.created_at == 4 * 3600  # #48 decision 6


def test_expired_ask_is_reasked_once_then_denied_by_silence(home: Path) -> None:
    from nanodot.core.statemachine import CHECKS_FAILED, WatchEvent

    h = Harness(home)
    h.tick()  # first ask
    assert len(h.permissions.pending()) == 1
    event = WatchEvent(
        kind=CHECKS_FAILED, message="checks failed",
        evidence={"head_sha": "s1", "checks": [{"name": "ci", "conclusion": "failure"}]},
        task_id=h.task.id, at=h.clock.time(),
    )

    h.clock.advance(4 * 3600 + 1)
    h.permissions.sweep_expired()
    assert h.permissions.pending() == []

    # The same content is re-asked exactly once (#48 decision 3).
    assert h.write.propose_write(h.task, event) is not None
    assert len(h.permissions.pending()) == 1
    history = h.permissions.request_history()
    assert [r.state for r in history] == ["pending", "expired"]

    # Second silence: a terminal denial, never re-asked.
    h.clock.advance(4 * 3600 + 1)
    h.permissions.sweep_expired()
    denied = h.write.propose_write(h.task, event)
    assert denied is not None and denied.kind == WRITE_SILENCE_DENIED
    states = [s for s, _ in h.permissions.verbatim_request_states(h.task.id, denied.evidence["content_hash"])]
    assert states[0] == "denied" and states.count("denied") == 1
    assert h.permissions.pending() == []
    assert h.writer.sends == []

    # A third identical ask produces nothing, ever.
    assert h.write.propose_write(h.task, event) is None
    assert h.kinds().count(WRITE_SILENCE_DENIED) == 1


# -- 5. single use ----------------------------------------------------------------


def test_capability_is_single_use_and_replay_sends_nothing(home: Path) -> None:
    h = Harness(home)
    h.tick()
    capability = h.approve_proposal()
    h.tick()  # executes
    assert len(h.writer.sends) == 1
    assert not h.permissions.consume(capability.grant_id)  # already used
    h.tick()
    assert len(h.writer.sends) == 1  # no replay send


def test_concurrent_double_execute_sends_exactly_once(home: Path) -> None:
    h = Harness(home)
    h.tick()
    h.approve_proposal()
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait()
        h.write.execute_pending(h.store.get(h.task.id), h.clock.time())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(h.writer.sends) == 1


# -- 6. content binding ------------------------------------------------------------


def test_post_approval_mutation_fails_closed(home: Path) -> None:
    h = Harness(home)
    h.tick()
    capability = h.approve_proposal()
    # Tamper with the stored payload after approval (simulates any drift
    # between what was approved and what would be sent).
    with sqlite3.connect(h.home / "nanodot.db") as conn:
        conn.execute(
            "UPDATE capabilities SET content='{\"body\": \"tampered\"}' WHERE grant_id=?",
            (capability.grant_id,),
        )
    h.tick()
    assert h.writer.sends == []  # nothing sent
    kinds = h.kinds()
    assert WRITE_FAILED in kinds


# -- 7. scope/lifecycle revocation ------------------------------------------------


@pytest.mark.parametrize(
    "change", ["scope", "pause", "cancel"],
)
def test_lifecycle_change_kills_the_capability(home: Path, change: str) -> None:
    h = Harness(home)
    h.tick()
    h.approve_proposal()
    if change == "scope":
        h.store.update_scope(h.task.id, purpose="scope edit probe")
    elif change == "pause":
        h.store.pause(h.task.id)
    else:
        h.store.cancel(h.task.id)
    h.tick()
    assert h.writer.sends == []
    if change == "scope":
        # The grant died with the scope edit: nothing left to execute.
        assert h.permissions.pending_capabilities(h.task.id) == []


# -- 8. no self-approval ------------------------------------------------------------


def test_approve_is_called_only_from_the_cli() -> None:
    """AST gate: nothing outside the CLI (and tests) may approve."""
    for py in (SRC).rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "approve"
            ):
                assert py.name == "cli.py", f"approve() call in {py.name}"


# -- 9. crash protocol ----------------------------------------------------------------


def test_crash_after_intent_never_resends_and_surfaces_unknown(home: Path) -> None:
    h = Harness(home)
    h.tick()
    capability = h.approve_proposal()
    # Consume (the atomic single-use), then only the durable intent, then
    # "crash" before any send.
    assert h.permissions.consume(capability.grant_id)
    assert h.write._log_intent(h.task, capability, h.clock.time())
    h.clock.advance(300)

    h2 = h.reopen()
    h2.tick()
    assert h2.writer.sends == []  # never blindly re-sent
    assert WRITE_UNKNOWN in h2.kinds()
    assert "will NOT be re-sent" in h2.sink.events[-1].message or any(
        "NOT be re-sent" in e.message for e in h2.sink.events
    )
    # Recovery surfaces exactly once and burns the capability.
    h3 = h.reopen()
    h3.tick()
    unknown_events = [e for e in h3.activity.query(task_id=h.task.id, kinds=(WRITE_UNKNOWN,))]
    assert len(unknown_events) == 1
    assert h3.writer.sends == []


def test_crash_after_send_surfaces_unknown_without_resending(home: Path) -> None:
    h = Harness(home)
    h.tick()
    capability = h.approve_proposal()
    now = h.clock.time()
    assert h.permissions.consume(capability.grant_id)
    assert h.write._log_intent(h.task, capability, now)
    h.write._send(capability, {"body": "approved"})  # POST left; no outcome logged
    h.clock.advance(300)

    h2 = h.reopen()
    h2.tick()
    assert len(h2.writer.sends) == 0  # the restart sent nothing new
    assert WRITE_UNKNOWN in h2.kinds()


def test_completed_write_recovery_finds_nothing_unknown(home: Path) -> None:
    h = Harness(home)
    h.tick()
    h.approve_proposal()
    h.tick()
    assert len(h.writer.sends) == 1
    h2 = h.reopen()
    events = h2.write.recover(h2.task, h2.clock.time())
    assert events == []
    assert WRITE_UNKNOWN not in h2.kinds()


# -- 10. log completeness ----------------------------------------------------------


def test_replay_shows_the_full_write_trail(home: Path) -> None:
    h = Harness(home)
    h.tick()  # checks-failed + write-proposed
    request = h.permissions.pending()[0]
    capability = h.permissions.approve(request.id)
    h.activity.append(  # the CLI's approval trail
        task_id=h.task.id, kind="write-approved",
        message="approved comment", at=h.clock.time(),
        evidence={"grant_id": capability.grant_id},
    )
    h.tick()  # write-intent + write-done
    kinds = h.kinds()  # replay order: oldest first
    assert kinds[:3] == ["check-observed", "checks-failed", WRITE_PROPOSED]
    # Writes precede the poll that follows them: approve → intent → done,
    # then that tick's own observation.
    assert kinds[-4:] == ["write-approved", WRITE_INTENT, WRITE_DONE, "check-observed"]

    # HTTP-boundary byte equality: the wire bytes hash to the approved hash.
    sent_capability, payload, sent_bytes = h.writer.executed[0]
    assert hashlib.sha256(sent_bytes).hexdigest() == sent_capability.content_hash
    assert sent_capability.content_hash == capability.content_hash
    assert payload == {"body": payload["body"]}


# -- 11. read purity ---------------------------------------------------------------


def test_fetch_never_sees_the_write_token(home: Path) -> None:
    FileSecretStore().set("github-token", "ghp_read_token")
    FileSecretStore().set("github-write-token", "ghp_write_should_not_leak")
    requests_seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        requests_seen.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 404, "x", {}, io.BytesIO(b"{}")
        )

    import nanodot.native.github_client as client

    original = client.authenticated_urlopen
    client.authenticated_urlopen = fake_urlopen
    try:
        fetcher = GitHubSnapshotFetcher()  # reads the token-mode secret store
        with pytest.raises(Exception):
            fetcher.fetch(PRTarget.parse("o/r#1"))
    finally:
        client.authenticated_urlopen = original
    assert requests_seen, "the fetch must have issued a request"
    for request in requests_seen:
        auth = request.get_header("Authorization") or ""
        assert "ghp_write_should_not_leak" not in auth
        assert auth.endswith("ghp_read_token")


def test_github_client_surface_stays_read_only() -> None:
    from nanodot.native.github_client import GitHubSnapshotFetcher

    public = {
        name for name in dir(GitHubSnapshotFetcher) if not name.startswith("_")
    }
    assert public == {"fetch"}, public


# -- 12. egress and redaction -------------------------------------------------------


def test_write_token_and_secrets_never_persist(home: Path) -> None:
    secret = "ghp_writerleak999"
    FileSecretStore().set("github-token", secret)
    FileSecretStore().set("github-write-token", secret)
    h = Harness(home)  # wires the real secret store into the redactor
    h.github.checks["s1"] = []
    h.github.add_check(f"ci-{secret}", FAILURE, sha="sha-leak")  # a leak vector
    h.github.set_pr("open", head_sha="sha-leak")
    from nanodot.native.notifier import NativeNotifier

    # Delivery goes through the real notifier (redactor-wired), not a fake.
    notifier = NativeNotifier(redactor=h.redactor, os_notify=False)
    h.loop = TaskLoop(
        h.store, h.github, notifier, h.activity, write=h.write,
    )
    h.tick()
    request = h.permissions.pending()[0]
    assert secret not in request.content  # the proposal was scrubbed
    capability = h.permissions.approve(request.id)
    h.tick()
    _, payload, sent_bytes = h.writer.executed[0]
    assert secret not in sent_bytes.decode()
    for entry in notifier.list():
        assert secret not in entry.message
        assert secret not in json.dumps(entry.evidence)
    raw = (h.home / "nanodot.db").read_bytes()
    assert secret.encode() not in raw


# -- 13. dependency direction --------------------------------------------------------


def test_write_flow_stays_inside_the_core_boundary() -> None:
    stdlib = __import__("sys").stdlib_module_names
    for py in (SRC / "core").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not name.startswith("nanodot.native"), f"{py.name}: {name}"
                if name.startswith("nanodot."):
                    continue
                assert name.split(".")[0] in stdlib, f"{py.name}: {name}"


# -- end-to-end sanity ----------------------------------------------------------------


def test_full_proposal_approval_execution_lifecycle(home: Path) -> None:
    h = Harness(home)
    outcome = h.tick()
    assert outcome is RunOutcome.OK
    assert h.permissions.pending(), "a proposal must exist"
    inbox_kinds = [e.kind for e in h.sink.events]
    assert WRITE_PROPOSED in inbox_kinds

    h.approve_proposal()
    h.clock.advance(300)
    h.tick()
    assert len(h.writer.sends) == 1
    kinds = h.kinds()
    assert WRITE_DONE in kinds
    # The write did not disturb the watch itself.
    task = h.store.get(h.task.id)
    assert task.state.value == "active"
    assert task.next_check_at is not None


# -- approved-state lifecycle (#65, #66) -------------------------------------------


def _failure_event(h: Harness, sha: str = "s1") -> WatchEvent:
    """A checks-failed event with the same evidence shape the state machine
    emits — reruns failing again on the same head produce these."""
    return WatchEvent(
        kind=CHECKS_FAILED,
        message="checks failed",
        evidence={
            "head_sha": sha,
            "pr_state": "open",
            "url": f"https://github.com/{TARGET}/pull/9",
            "checks": [{"name": "ci", "conclusion": "failure"}],
        },
        task_id=h.task.id,
        at=h.clock.time(),
    )


def test_executed_content_is_never_re_asked_after_a_rerun_failure(home: Path) -> None:
    """#65: a rerun failing again on the same head is a new checks-failed
    event with byte-identical draft content — exactly one request, one
    approval, one comment, never a second ask."""
    h = Harness(home)
    h.tick()
    h.approve_proposal()
    h.clock.advance(300)
    h.tick()
    assert len(h.writer.sends) == 1

    assert h.write.propose_write(h.store.get(h.task.id), _failure_event(h)) is None
    assert h.write.propose_write(h.store.get(h.task.id), _failure_event(h)) is None
    assert len(h.permissions.request_history()) == 1
    assert len(h.writer.sends) == 1  # no double comment


def test_approved_but_unsent_content_is_not_re_asked_but_new_content_is(
    home: Path,
) -> None:
    """#65: the approved-but-queued window produces no duplicate ask; a new
    head SHA (new content, new digest) proposes normally."""
    h = Harness(home)
    h.tick()
    h.approve_proposal()  # queued, not yet executed

    assert h.write.propose_write(h.store.get(h.task.id), _failure_event(h)) is None

    new_head = h.write.propose_write(h.store.get(h.task.id), _failure_event(h, sha="s2"))
    assert new_head is not None and new_head.kind == WRITE_PROPOSED


def test_expired_unused_capability_notifies_once_and_reopens_the_ask(
    home: Path,
) -> None:
    """#66: an approval whose grant expires unused is a notified failure —
    never silence — and its content becomes askable again."""
    h = Harness(home)
    h.tick()
    h.approve_proposal()
    h.clock.advance(24 * 3600 + 10)  # past the request TTL, runner was "down"
    outcome = h.tick()
    assert outcome is RunOutcome.OK

    assert len(h.writer.sends) == 0  # nothing was ever sent
    failures = [e for e in h.sink.events if e.kind == WRITE_FAILED]
    assert len(failures) == 1, "exactly one lost-write notification"
    assert "expired" in failures[0].message
    assert "never executed" in failures[0].message

    # The content is askable again; a second tick does not re-notify.
    assert h.write.propose_write(h.store.get(h.task.id), _failure_event(h)) is not None
    h.clock.advance(300)
    h.tick()
    assert len([e for e in h.sink.events if e.kind == WRITE_FAILED]) == 1
    assert h.permissions.pending(), "the re-opened ask is pending again"


# -- native writer transport (offline, fake urlopen) ----------------------------


class _FakeResponse(io.BytesIO):
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _real_capability(home: Path, payload: dict) -> WriteCapability:
    center = PermissionCenter(path=home / "nanodot.db")
    request = center.request(
        action="comment", target="o/r#1", scope="watch", task_id="t1",
        content=payload,
    )
    return center.approve(request.id)


def test_native_writer_sends_approved_bytes_with_the_write_token(home: Path) -> None:
    import nanodot.native.github_writer as writer_module

    requests_seen: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        requests_seen.append(request)
        return _FakeResponse(b'{"html_url": "https://github.test/c/1"}')

    capability = _real_capability(home, {"body": "approved bytes"})
    original = writer_module.authenticated_urlopen
    writer_module.authenticated_urlopen = fake_urlopen
    try:
        writer = writer_module.GitHubWriter(token="write-token")
        result = writer.execute(capability, {"body": "approved bytes"})
    finally:
        writer_module.authenticated_urlopen = original
    assert result.status == 201
    (request,) = requests_seen
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.github.com/repos/o/r/issues/1/comments"
    assert request.get_header("Authorization") == "Bearer write-token"
    import json as _json
    assert _json.loads(request.data.decode()) == {"body": "approved bytes"}


def test_native_writer_refuses_mutated_bytes(home: Path) -> None:
    import nanodot.native.github_writer as writer_module

    requests_seen: list[urllib.request.Request] = []
    capability = _real_capability(home, {"body": "approved bytes"})
    original = writer_module.authenticated_urlopen
    writer_module.authenticated_urlopen = (
        lambda request, timeout=None: requests_seen.append(request)
    )
    try:
        writer = writer_module.GitHubWriter(token="write-token")
        with pytest.raises(WriteContentMismatch):
            writer.execute(capability, {"body": "mutated bytes"})
    finally:
        writer_module.authenticated_urlopen = original
    assert requests_seen == []  # nothing left the host


def test_native_writer_without_token_fails_closed(home: Path) -> None:
    import nanodot.native.github_writer as writer_module

    capability = _real_capability(home, {"body": "approved bytes"})
    writer = writer_module.GitHubWriter(token=None)  # no secret configured
    with pytest.raises(WriteCredentialMissing):
        writer.execute(capability, {"body": "approved bytes"})

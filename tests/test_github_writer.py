"""GitHub writer port + transport acceptance tests (issue #51).

All offline: the transport's urlopen is faked, the credential store is the
real file-backed one under an isolated home. No write action exists yet —
the action table is injected so the transport itself is exercisable.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from nanodot.core.tasks import PRTarget
from nanodot.native.github_client import GitHubSnapshotFetcher
from nanodot.native.github_writer import WRITE_TOKEN_SECRET, GitHubWriter
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github_writer import (
    CAPABILITY_FIELDS,
    WriteActionUnknown,
    WriteCapability,
    WriteContentMismatch,
    WriteCredentialMissing,
    WriteError,
    WriteRejectedError,
    WriteTransportError,
    payload_digest,
)
from nanodot.ports.github import RetryableError  # noqa: F401 (read-side sanity)

TEST_ACTIONS = {
    "test-comment": ("POST", "/repos/{owner}/{repo}/issues/{number}/comments"),
}
TARGET = "thinkflowlab/nanodot#51"
PAYLOAD = {"body": "retested; flaky X passes on the current head"}


def capability(payload: dict = PAYLOAD, action: str = "test-comment") -> WriteCapability:
    return WriteCapability(
        action=action,
        target=TARGET,
        content_hash=payload_digest(payload),
        grant_id="grant-1",
    )


class _Response(io.BytesIO):
    def __init__(self, body: dict | str, status: int = 201) -> None:
        raw = body if isinstance(body, str) else json.dumps(body)
        super().__init__(raw.encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class TransportSpy:
    """Records the request; fails the test if told the call was forbidden."""

    def __init__(self, response=None, error=None, forbidden: bool = False) -> None:
        self.requests: list = []
        self.response = response
        self.error = error
        self.forbidden = forbidden

    def __call__(self, request, timeout=None):
        assert not self.forbidden, "transport used where it must not be"
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def patch_transport(monkeypatch, spy: TransportSpy) -> None:
    monkeypatch.setattr(
        "nanodot.native.github_writer.authenticated_urlopen", spy
    )


def make_writer(token: str = "ghp_write_token") -> GitHubWriter:
    return GitHubWriter(token=token, actions=TEST_ACTIONS)


# -- port surface freeze (docs/design/github-writer.md) ------------------------


def test_port_surface_is_frozen() -> None:
    import inspect

    from nanodot.ports import github_writer as port

    protocol_methods = {
        name for name, member in inspect.getmembers(port.GitHubWriter)
        if not name.startswith("_") and callable(member)
    }
    assert protocol_methods == {"execute"}
    assert CAPABILITY_FIELDS == {
        "action", "target", "content_hash", "grant_id", "used_at"
    }


# -- fail closed before any socket use ------------------------------------------


def test_unknown_action_never_touches_transport(home: Path, monkeypatch) -> None:
    spy = TransportSpy(forbidden=True)
    patch_transport(monkeypatch, spy)
    writer = make_writer()
    with pytest.raises(WriteActionUnknown):
        writer.execute(capability(action="merge"), PAYLOAD)
    assert spy.requests == []


def test_missing_credential_raises_before_socket_use(
    home: Path, monkeypatch
) -> None:
    spy = TransportSpy(forbidden=True)
    patch_transport(monkeypatch, spy)
    writer = GitHubWriter(actions=TEST_ACTIONS)  # no token, empty store
    with pytest.raises(WriteCredentialMissing, match="write token"):
        writer.execute(capability(), PAYLOAD)
    assert spy.requests == []


def test_content_mismatch_fails_closed(home: Path, monkeypatch) -> None:
    spy = TransportSpy(forbidden=True)
    patch_transport(monkeypatch, spy)
    writer = make_writer()
    tampered = capability(payload={**PAYLOAD, "body": "different text"})
    with pytest.raises(WriteContentMismatch, match="content hash"):
        writer.execute(tampered, {**PAYLOAD, "body": "different text 2"})
    assert spy.requests == []


# -- the wire: pinned destination, write credential, exact bytes -----------------


def test_execute_sends_exact_approved_bytes_with_write_token(
    home: Path, monkeypatch
) -> None:
    FileSecretStore().set("github-token", "ghp_read_token")
    FileSecretStore().set(WRITE_TOKEN_SECRET, "ghp_write_token")
    spy = TransportSpy(response=_Response({"id": 42}))
    patch_transport(monkeypatch, spy)

    # No explicit token: the writer must find github-write-token itself.
    writer = GitHubWriter(actions=TEST_ACTIONS)
    result = writer.execute(capability(), PAYLOAD)

    assert result.status == 201 and result.body == {"id": 42}
    assert result.action == "test-comment"
    (request,) = spy.requests
    assert request.get_method() == "POST"
    assert request.full_url == (
        "https://api.github.com/repos/thinkflowlab/nanodot/issues/51/comments"
    )
    # Blast-radius split: the write token, never the read token.
    assert request.headers["Authorization"] == "Bearer ghp_write_token"
    # The bytes on the wire hash-equal the approved content hash.
    sent = request.data
    assert sent == json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(sent).hexdigest() == capability().content_hash


def test_error_mapping(home: Path, monkeypatch) -> None:
    cases = [
        (HTTPError("u", 302, "redirect", {}, io.BytesIO(b"")), WriteTransportError),
        (HTTPError("u", 403, "nope", {}, io.BytesIO(b"forbidden")), WriteRejectedError),
        (HTTPError("u", 404, "nope", {}, io.BytesIO(b"missing")), WriteRejectedError),
        (HTTPError("u", 502, "bad", {}, io.BytesIO(b"gateway")), WriteTransportError),
        (OSError("unreachable"), WriteTransportError),
    ]
    for error, expected in cases:
        spy = TransportSpy(error=error)
        patch_transport(monkeypatch, spy)
        writer = make_writer()
        with pytest.raises(expected):
            writer.execute(capability(), PAYLOAD)


def test_unparseable_response_is_typed(home: Path, monkeypatch) -> None:
    spy = TransportSpy(response=_Response("<html>oops", status=200))
    patch_transport(monkeypatch, spy)
    with pytest.raises(WriteTransportError):
        make_writer().execute(capability(), PAYLOAD)


def test_action_path_cannot_escape_destination(home: Path, monkeypatch) -> None:
    spy = TransportSpy(forbidden=True)
    patch_transport(monkeypatch, spy)
    writer = GitHubWriter(
        token="t",
        actions={"evil": ("POST", "https://evil.example/{owner}")},
    )
    with pytest.raises((WriteActionUnknown, WriteError)):
        writer.execute(capability(action="evil"), PAYLOAD)
    assert spy.requests == []


# -- read side stays pure ---------------------------------------------------------


def test_read_client_unchanged_get_only(home: Path) -> None:
    public = {
        name for name in dir(GitHubSnapshotFetcher) if not name.startswith("_")
    }
    assert "execute" not in public
    assert public == {"fetch"}, public


# -- the fake writer mirrors the contract ------------------------------------------


def test_fake_writer_records_and_enforces_hash() -> None:
    from fakes import FakeGitHubWriter

    fake = FakeGitHubWriter()
    result = fake.execute(capability(), PAYLOAD)
    assert result.status == 201
    (capability_sent, payload_sent, bytes_sent) = fake.executed[0]
    assert payload_sent == PAYLOAD
    assert hashlib.sha256(bytes_sent).hexdigest() == capability_sent.content_hash

    with pytest.raises(WriteContentMismatch):
        fake.execute(capability(), {"body": "mutated"})  # hash bound to PAYLOAD


def test_fake_writer_scriptable_failure() -> None:
    from fakes import FakeGitHubWriter

    fake = FakeGitHubWriter()
    fake.respond_with(WriteRejectedError("403"))
    with pytest.raises(WriteRejectedError):
        fake.execute(capability(), PAYLOAD)

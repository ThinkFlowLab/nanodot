"""Write credential and permission-mode acceptance tests (issue #52).

The separated github-write-token with set-time validation (fail closed),
the canonical permission-mode key, and the no-token invariant: without a
write credential no mode can produce a writer execution.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from nanodot.core.config import Config
from nanodot.core.permissions import Mode, PermissionCenter, WriteForbidden
from nanodot.native.github_writer import GitHubWriter, probe_write_token
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github_writer import (
    WriteCredentialMissing,
    WriteRejectedError,
    WriteTransportError,
)

READ = "ghp_read_token_123"
WRITE = "github_pat_write_token_456"


def set_write_token(monkeypatch, *, probe=None) -> None:
    """CLI path for storing the write token, with the probe controlled."""
    from nanodot.cli import main

    if probe is not None:
        monkeypatch.setattr(
            "nanodot.native.github_writer.probe_write_token", probe
        )
    code = main(["config", "set", "github-write-token", WRITE])
    assert code == 0


# -- set-time validation, fail closed -----------------------------------------


def test_write_token_identical_to_read_token_rejected(
    home: Path, monkeypatch, capsys
) -> None:
    from nanodot.cli import main

    FileSecretStore().set("github-token", READ)
    monkeypatch.setattr(
        "nanodot.native.github_writer.probe_write_token",
        lambda token: pytest.fail("probe must not run for a duplicate token"),
    )
    assert main(["config", "set", "github-write-token", READ]) == 1
    assert "separate credential" in capsys.readouterr().err
    assert FileSecretStore().get("github-write-token") is None  # nothing stored


@pytest.mark.parametrize(
    "probe_error, message",
    [
        (WriteRejectedError("GitHub rejected the write token (401)"), "rejected"),
        (WriteTransportError("cannot verify the write token: offline"), "verify"),
    ],
)
def test_failed_probe_stores_nothing(
    home: Path, monkeypatch, capsys, probe_error, message
) -> None:
    from nanodot.cli import main

    def probe(token):
        raise probe_error

    monkeypatch.setattr("nanodot.native.github_writer.probe_write_token", probe)
    assert main(["config", "set", "github-write-token", WRITE]) == 1
    err = capsys.readouterr().err
    assert message in err and "nothing stored" in err
    assert FileSecretStore().get("github-write-token") is None


def test_valid_write_token_stored_masked_unset(
    home: Path, monkeypatch, capsys
) -> None:
    from nanodot.cli import main

    set_write_token(monkeypatch, probe=lambda token: {"login": "me"})
    assert FileSecretStore().get("github-write-token") == WRITE

    capsys.readouterr()
    assert main(["config", "list"]) == 0
    out = capsys.readouterr().out
    assert WRITE not in out and "github-write-token=***" in out

    assert main(["config", "unset", "github-write-token"]) == 0
    assert FileSecretStore().get("github-write-token") is None


# -- the probe itself (offline, faked transport) -------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: dict, status: int = 200) -> None:
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def test_probe_read_only_and_typed_errors(monkeypatch) -> None:
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _Response({"login": "write-bot"})

    monkeypatch.setattr(
        "nanodot.native.github_writer.authenticated_urlopen", fake_urlopen
    )
    assert probe_write_token(WRITE) == {"login": "write-bot"}
    assert seen[0].get_method() == "GET"  # the probe can never write

    cases = [
        (HTTPError("u", 401, "bad", {}, io.BytesIO(b"")), WriteRejectedError),
        (HTTPError("u", 302, "redirect", {}, io.BytesIO(b"")), WriteTransportError),
        (HTTPError("u", 503, "down", {}, io.BytesIO(b"")), WriteTransportError),
        (OSError("offline"), WriteTransportError),
    ]
    for error, expected in cases:
        monkeypatch.setattr(
            "nanodot.native.github_writer.authenticated_urlopen",
            lambda request, timeout=None: (_ for _ in ()).throw(error),
        )
        with pytest.raises(expected):
            probe_write_token(WRITE)


# -- permission-mode: canonical key, legacy fallback, fail-closed load ----------


def test_permission_mode_defaults_to_auto(home: Path) -> None:
    # #48 decision 5: auto is the default — inert without grants/write token.
    assert PermissionCenter().mode() is Mode.AUTO


def test_permission_mode_canonical_key_and_legacy_fallback(home: Path) -> None:
    Config().set("permission-mode", "gated")
    assert PermissionCenter().mode() is Mode.GATED

    # A pre-rename install with the legacy key still loads.
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"mode": "gated"}))
    assert PermissionCenter().mode() is Mode.GATED


def test_permission_mode_accepts_auto_and_rejects_garbage(home: Path) -> None:
    Config().set("permission-mode", "auto")
    assert PermissionCenter().mode() is Mode.AUTO
    with pytest.raises(ValueError, match="must be readonly, gated, or auto"):
        Config().set("permission-mode", "yolo")
    Config().set("mode", "auto")  # the legacy key accepts it too
    assert PermissionCenter().mode() is Mode.AUTO


def test_readonly_hard_off_with_write_token_configured(
    home: Path, monkeypatch
) -> None:
    set_write_token(monkeypatch, probe=lambda token: {"login": "me"})
    Config().set("permission-mode", "readonly")  # mode is never token-derived
    center = PermissionCenter()
    assert center.mode() is Mode.READONLY
    with pytest.raises(WriteForbidden):
        center.assert_allowed("comment")
    center.assert_allowed("read")


# -- the no-token invariant ------------------------------------------------------


def test_no_write_token_means_no_execution_in_any_mode(
    home: Path, monkeypatch
) -> None:
    from nanodot.ports.github_writer import WriteCapability, payload_digest

    monkeypatch.setattr(
        "nanodot.native.github_writer.authenticated_urlopen",
        lambda request, timeout=None: pytest.fail("no socket use is allowed"),
    )
    writer = GitHubWriter(token=None)  # the store has no write token
    capability = WriteCapability(
        action="comment",
        target="thinkflowlab/nanodot#52",
        content_hash=payload_digest({"body": "x"}),
        grant_id="grant-1",
    )
    for mode_value in ("readonly", "gated"):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(
            json.dumps({"permission-mode": mode_value})
        )
        with pytest.raises(WriteCredentialMissing):
            writer.execute(capability, {"body": "x"})

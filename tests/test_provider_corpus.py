"""Provider malformed-response corpus + era-jump migration (issue #105).

Real APIs send dirty data; the contract tests use ideal shapes. Every
corpus row asserts the same three things per adapter: ProviderError
(fail closed), no crash, and no credential material in the error text.
The migration test proves a v0.1-era database opens under current code
with every newer dimension off.
"""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from fakes import FakeClock, FakeGitHub, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.runner import TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.native.providers.anthropic import AnthropicProvider
from nanodot.native.providers.openai_compat import APIInferenceProvider
from nanodot.ports.inference import ProviderError, StateChange

CHANGE = StateChange(kind="checks-failed", summary="ci failing", head_sha="s")
API_KEY = "sk-corpus-secret-123"


class _Response(io.BytesIO):
    def __init__(self, body: bytes | str, status: int = 200) -> None:
        raw = body if isinstance(body, bytes) else body.encode()
        super().__init__(raw)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


OPENAI_GOOD = {"choices": [{"message": {"content": "ok"}}]}
ANTHROPIC_GOOD = {"content": [{"type": "text", "text": "ok"}]}

# (label, raw body, status) — protocol-agnostic dirt.
CORPUS = [
    ("truncated-json", b'{"choices": [{"message"'),
    ("empty-body", b""),
    ("html-error-page", b"<html><body>502 Bad Gateway</body></html>"),
    ("null-instead-of-object", b"null"),
    ("array-instead-of-object", b"[1, 2, 3]"),
    ("success-status-error-shape", json.dumps({"error": {"message": "nope"}})),
    ("wrong-inner-shape", json.dumps({"choices": []})),
    ("null-content-field", json.dumps({"choices": [{"message": {"content": None}}]})),
    (
        "anthropic-text-not-string",
        json.dumps({"content": [{"type": "text", "text": 42}]}),
    ),
    ("anthropic-no-text-block", json.dumps({"content": [{"type": "image"}]})),
    ("empty-text", json.dumps({"choices": [{"message": {"content": "   "}}]})),
]


def _patch_transport(monkeypatch, module, body, status=200):
    def fake_urlopen(request, timeout=None):
        return _Response(body, status=status)

    monkeypatch.setattr(module, "authenticated_urlopen", fake_urlopen)


@pytest.mark.parametrize("label,body", [(c[0], c[1]) for c in CORPUS])
def test_openai_compat_corpus_fails_closed(monkeypatch, label, body) -> None:
    import nanodot.native.providers.openai_compat as module

    _patch_transport(monkeypatch, module, body)
    provider = APIInferenceProvider(api_key=API_KEY, base_url="https://api.test/v1")
    with pytest.raises(ProviderError) as caught:
        provider.summarize(CHANGE)
    assert API_KEY not in str(caught.value)


@pytest.mark.parametrize("label,body", [(c[0], c[1]) for c in CORPUS])
def test_anthropic_corpus_fails_closed(monkeypatch, label, body) -> None:
    import nanodot.native.providers.anthropic as module

    _patch_transport(monkeypatch, module, body)
    provider = AnthropicProvider(api_key=API_KEY, base_url="https://api.test")
    with pytest.raises(ProviderError) as caught:
        provider.summarize(CHANGE)
    assert API_KEY not in str(caught.value)


@pytest.mark.parametrize("body", [b'{"choices":[{}]}', b"", b"<html>upstream</html>"])
def test_parse_intent_corpus_never_returns_garbage(monkeypatch, body) -> None:
    import nanodot.native.providers.openai_compat as module

    _patch_transport(monkeypatch, module, body)
    provider = APIInferenceProvider(api_key=API_KEY, base_url="https://api.test/v1")
    with pytest.raises(ProviderError):
        provider.parse_intent("watch owner/repo#1")


def test_good_shapes_still_pass_through_both_adapters(monkeypatch) -> None:
    """The corpus must not overfit: the ideal shapes keep working."""
    import nanodot.native.providers.anthropic as amod
    import nanodot.native.providers.openai_compat as omod

    _patch_transport(monkeypatch, omod, json.dumps(OPENAI_GOOD).encode())
    assert APIInferenceProvider(
        api_key=API_KEY, base_url="https://api.test/v1"
    ).summarize(CHANGE) == "ok"

    _patch_transport(monkeypatch, amod, json.dumps(ANTHROPIC_GOOD).encode())
    assert AnthropicProvider(
        api_key=API_KEY, base_url="https://api.test"
    ).summarize(CHANGE) == "ok"


# -- era-jump migration -----------------------------------------------------------


_V01_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  target TEXT NOT NULL,
  purpose TEXT NOT NULL,
  cadence_seconds INTEGER NOT NULL,
  allowed_actions TEXT NOT NULL,
  notification_conditions TEXT NOT NULL,
  stop_conditions TEXT NOT NULL,
  state TEXT NOT NULL,
  blocker TEXT,
  scope_version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  next_check_at REAL,
  watch_state TEXT NOT NULL DEFAULT '{}'
);
"""
_V01_PERMISSIONS = """
CREATE TABLE IF NOT EXISTS grants (
  id TEXT PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL,
  scope TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL NOT NULL,
  expires_at REAL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL,
  scope TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL NOT NULL,
  expires_at REAL NOT NULL, state TEXT NOT NULL
);
"""


def test_v01_database_jumps_to_current_schema(home: Path) -> None:
    db = home / "nanodot.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(_V01_TASKS + _V01_PERMISSIONS)
    from nanodot.core.tasks import (
        DEFAULT_NOTIFICATION_CONDITIONS,
        DEFAULT_STOP_CONDITIONS,
    )

    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy1", "o/r#1", "legacy", 300, '["read"]',
            DEFAULT_NOTIFICATION_CONDITIONS, DEFAULT_STOP_CONDITIONS,
            "active", None, 1, 1.0, 1.0, 1.0, "{}",
        ),
    )
    conn.commit()
    conn.close()

    store = TaskStore(path=db)
    task = store.get("legacy1")
    assert task is not None
    # Every newer dimension is off, and the legacy row round-trips.
    assert task.digest_interval_seconds is None
    assert task.stale_after_seconds is None
    assert task.flaky_alerts is False
    task.purpose = "still works"
    store.update(task)
    assert store.get("legacy1").purpose == "still works"

    from nanodot.core.permissions import PermissionCenter

    center = PermissionCenter(path=db)
    assert center.grants() == []  # old tables migrated, readable
    # Reopen: the additive migrations are idempotent.
    store.close()
    center.close()
    store2 = TaskStore(path=db)
    assert store2.get("legacy1").digest_interval_seconds is None
    store2.close()

"""Inference provider + egress acceptance tests (issue #11)."""

from __future__ import annotations

import io
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from fakes import FAILURE, FakeClock, FakeGitHub, FakeProvider, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.egress import EgressGuard, SUMMARIZE_FIELDS
from nanodot.core.redaction import Redactor
from nanodot.core.runner import RunOutcome, TaskLoop, state_change_from_event
from nanodot.core.statemachine import CHECKS_FAILED, WatchEvent
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.native.inference_api import APIInferenceProvider, configured_provider
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.inference import ProviderError, StateChange, TaskDraft

TARGET = PRTarget.parse("thinkflowlab/nanodot#5")
API_KEY = "sk-inference-key-42"


# -- egress guard: the whitelist -------------------------------------------


def test_summarize_payload_is_exactly_the_whitelist() -> None:
    guard = EgressGuard()
    change = StateChange(
        kind=CHECKS_FAILED,
        summary="ci failing",
        head_sha="abc",
        pr_state="open",
        url="https://github.com/x/y/pull/5",
        checks=(("ci", "failure"), ("lint", "success")),
    )
    payload = guard.summarize_request(change)
    assert set(payload) == {"kind", "summary", "head_sha", "pr_state", "url", "checks"}
    assert payload["checks"] == [
        {"name": "ci", "conclusion": "failure"},
        {"name": "lint", "conclusion": "success"},
    ]


def test_intent_payload_is_user_text_only() -> None:
    payload = EgressGuard().intent_request("watch owner/repo#9 until checks pass")
    assert set(payload) == {"intent_text"}


def test_outbound_values_are_scrubbed_of_secrets(home: Path) -> None:
    FileSecretStore().set("api-key", API_KEY)
    guard = EgressGuard(redactor=Redactor(FileSecretStore()))
    payload = guard.summarize_request(
        StateChange(kind="x", summary=f"failure mentioning {API_KEY}")
    )
    assert API_KEY not in payload["summary"]


# -- egress tripwire: provider-visible implies activity-logged --------------


def test_egress_is_a_projection_of_the_activity_log(home: Path) -> None:
    """Adapted from DSH's 'model-visible ⟺ logged' invariant
    (docs/design/adapter-seam.md, admission discipline 3): every field that
    reaches the provider is reconstructable from the activity log. Widening
    the provider-visible surface must fail here until it is whitelisted in
    docs/design/egress.md and recorded to the log."""
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")
    sink = FakeSink()
    provider = FakeProvider()
    loop = TaskLoop(store, github, sink, activity, provider=provider)
    task = store.create(
        Task(target=TARGET, purpose="watch", cadence_seconds=300, next_check_at=0.0)
    )

    assert loop.run_once(store.get(task.id), 0.0) is RunOutcome.OK
    assert provider.summarize_payloads, "a notable event was summarized"

    entries = activity.query(task_id=task.id, limit=1000)
    for change in provider.summarize_payloads:
        # The StateChange surface is exactly the documented whitelist.
        assert set(asdict(change)) == set(SUMMARIZE_FIELDS)
        # ...and every payload is reconstructable from a log entry.
        reconstructed = [
            state_change_from_event(
                WatchEvent(kind=e.kind, message=e.message, evidence=dict(e.evidence))
            )
            for e in entries
            if e.kind == change.kind and e.message == change.summary
        ]
        assert change in reconstructed, (
            f"provider saw {change.kind!r} that the activity log cannot reconstruct"
        )


# -- API adapter: same interface, egress-controlled body --------------------


def _chat_response(content: str) -> io.BytesIO:
    body = {"choices": [{"message": {"content": content}}]}
    return io.BytesIO(json.dumps(body).encode())


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_api_adapter_summarize_and_parse(monkeypatch) -> None:
    sent: list[dict] = []

    def fake_urlopen(request, timeout=None):
        sent.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.data.decode()),
            }
        )
        if "/chat/completions" in request.full_url:
            user_msg = sent[-1]["body"]["messages"][1]["content"]
            if "intent_text" in user_msg:
                return _chat_response(
                    json.dumps({"target": "o/r#2", "purpose": "watch"})
                )
            return _chat_response("model summary")

    monkeypatch.setattr(
        "nanodot.native.providers.openai_compat.authenticated_urlopen", fake_urlopen
    )
    provider = APIInferenceProvider(api_key=API_KEY, base_url="https://model.test/v1",
                                    model="m1")

    assert provider.summarize(StateChange(kind="k", summary="s")) == "model summary"
    draft = provider.parse_intent("please watch o/r#2")
    assert draft == TaskDraft(target="o/r#2", purpose="watch",
                              raw='{"target": "o/r#2", "purpose": "watch"}')

    for call in sent:
        assert call["method"] == "POST"
        assert call["auth"] == f"Bearer {API_KEY}"  # header only, never the body
        body_text = json.dumps(call["body"])
        assert API_KEY not in body_text
        # The user message contains only guard-built payloads.
        user_content = call["body"]["messages"][1]["content"]
        parsed = json.loads(user_content)
        allowed = {"kind", "summary", "head_sha", "pr_state", "url", "checks",
                   "intent_text"}
        assert set(parsed) <= allowed, parsed


def test_api_adapter_errors_are_provider_errors(monkeypatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise OSError("no network")

    monkeypatch.setattr(
        "nanodot.native.providers.openai_compat.authenticated_urlopen", fake_urlopen
    )
    provider = APIInferenceProvider(api_key=API_KEY)
    with pytest.raises(ProviderError):
        provider.summarize(StateChange(kind="k", summary="s"))
    malformed = APIInferenceProvider(api_key=API_KEY)

    def bad_json(request, timeout=None):
        return _Response(b"not json at all")

    monkeypatch.setattr(
        "nanodot.native.providers.openai_compat.authenticated_urlopen", bad_json
    )
    with pytest.raises(ProviderError):
        malformed.parse_intent("watch something")


def test_configured_provider_requires_full_config(home: Path) -> None:
    from nanodot.core.config import Config

    assert configured_provider() is None
    FileSecretStore().set("api-key", API_KEY)
    assert configured_provider() is None
    config = Config()
    config.set("model-base-url", "https://model.test/v1")
    config.set("model-name", "m1")
    assert configured_provider() is not None


# -- degraded mode: the watch never depends on the model ---------------------


def _loop_with(provider, home: Path):
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")
    sink = FakeSink()
    task = store.create(Task(target=TARGET, purpose="p", next_check_at=0.0))
    loop = TaskLoop(store, github, sink, activity, provider=provider)
    return loop, store, sink, task


def test_provider_failure_degrades_to_raw_message(home: Path) -> None:
    provider = FakeProvider()
    provider.fail_summaries = True
    loop, store, sink, task = _loop_with(provider, home)
    loop.run_once(store.get(task.id), FakeClock().now)

    assert sink.kinds() == [CHECKS_FAILED]  # still notified
    assert sink.events[0].summary is None   # no summary, raw message intact
    assert "failing" in sink.events[0].message


def test_provider_summary_decorates_notification(home: Path) -> None:
    provider = FakeProvider()
    loop, store, sink, task = _loop_with(provider, home)
    loop.run_once(store.get(task.id), FakeClock().now)

    assert sink.events[0].summary == "A short model summary."
    # The activity log keeps the raw message as the record of truth.
    from nanodot.core.activity import ActivityLog as AL

    entries = AL(path=home / "nanodot.db").query(task_id=task.id)
    assert "failing" in entries[0].message


def test_no_provider_means_no_egress_attempt(home: Path) -> None:
    loop, store, sink, task = _loop_with(None, home)
    loop.run_once(store.get(task.id), FakeClock().now)
    assert sink.kinds() == [CHECKS_FAILED]


# -- CLI intent path -----------------------------------------------------------


def test_cli_intent_without_model_falls_back(home: Path, capsys) -> None:
    from nanodot.cli import main

    FileSecretStore().set("github-token", "ghp_x")
    code = main(["watch", "add", "o/r#3", "--intent", "watch o/r#3", "--yes"])
    assert code == 0
    err = capsys.readouterr().err
    assert "no model configured" in err
    assert len(TaskStore().list()) == 1


def test_cli_intent_with_model_parses(home: Path, capsys, monkeypatch) -> None:
    from nanodot.cli import main
    from nanodot.core.config import Config

    FileSecretStore().set("github-token", "ghp_x")
    FileSecretStore().set("api-key", API_KEY)
    Config().set("model-base-url", "https://model.test/v1")
    Config().set("model-name", "m1")

    fake = FakeProvider()
    fake.draft = TaskDraft(target="owner/repo#9", purpose="watch until green")
    # The CLI imports configured_provider at call time, so patching the
    # module attribute takes effect.
    monkeypatch.setattr(
        "nanodot.native.providers.configured_provider", lambda: fake
    )

    code = main(["watch", "add", "--intent", "watch owner/repo#9", "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "owner/repo#9" in out
    tasks = TaskStore().list()
    assert str(tasks[0].target) == "owner/repo#9"
    assert tasks[0].purpose == "watch until green"


def test_slow_summary_does_not_hold_up_other_tasks(home):
    import threading
    import time

    release = threading.Event()
    calls = []

    class SlowProvider:
        def summarize(self, change):
            calls.append(change)
            release.wait(2)
            return "late summary"

    provider = SlowProvider()
    loop, store, sink, first = _loop_with(provider, home)
    # Small deterministic test budget. Production uses one second.
    from nanodot.core.runner import _SummaryBudget
    loop._summaries = _SummaryBudget(0.01)
    second = store.create(Task(target=TARGET, purpose="another", next_check_at=0.0))
    try:
        before = time.monotonic()
        loop.run_once(first, FakeClock().now)
        loop.run_once(second, FakeClock().now)
        # Must stay well under the provider's 2s hang to prove the next run
        # was not held up — but generous for a loaded CI runner (this exact
        # bound flaked at 0.84s once); independence is also proven by the
        # calls/sink assertions below.
        assert time.monotonic() - before < 1.5
        assert len(calls) == 1  # no unbounded backlog/threads while previous hangs
        assert len(sink.events) == 2
        assert all(event.summary is None for event in sink.events)
    finally:
        release.set()


def test_unexpected_summary_exception_degrades_to_raw_notification(home):
    class BrokenProvider:
        def summarize(self, change):
            raise KeyError("malformed adapter")

    loop, store, sink, task = _loop_with(BrokenProvider(), home)
    loop.run_once(task, FakeClock().now)
    assert len(sink.events) == 1
    assert sink.events[0].summary is None


def test_cli_runner_wiring_uses_configured_provider(home, monkeypatch):
    """The real CLI factory must enable summaries as soon as inference exists."""
    from nanodot.cli import _wiring

    provider = FakeProvider()
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")
    sink = FakeSink()
    monkeypatch.setattr("nanodot.native.providers.configured_provider", lambda: provider)
    monkeypatch.setattr("nanodot.native.github_client.GitHubSnapshotFetcher", lambda: github)
    monkeypatch.setattr("nanodot.native.notifier.NativeNotifier", lambda **kwargs: sink)
    # Provider wiring only: keep the write flow (auto default) out of the sink.
    from nanodot.core.config import Config
    Config().set("permission-mode", "readonly")

    _, store, _, _, _, loop = _wiring()
    task = store.create(Task(target=TARGET, purpose="watch", next_check_at=0.0))
    loop.run_once(task, FakeClock().now)

    assert len(provider.summarize_payloads) == 1
    assert sink.kinds() == [CHECKS_FAILED]
    assert sink.events[0].summary == "A short model summary."


def test_truncated_response_body_is_a_provider_error(monkeypatch) -> None:
    import http.client

    class _Truncated(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            raise http.client.IncompleteRead(b"{partial")

    monkeypatch.setattr(
        "nanodot.native.providers.openai_compat.authenticated_urlopen",
        lambda request, timeout=None: _Truncated(b""),
    )
    provider = APIInferenceProvider(api_key=API_KEY)
    with pytest.raises(ProviderError, match="unreachable"):
        provider.parse_intent("watch owner/repo#1")

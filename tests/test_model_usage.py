"""Model usage accounting and the opt-in daily budget (issue #46 P1).

Offline: faked transports only. The budget fails closed as ProviderError
— the same degrade path as any provider unavailability — and never
silently drops a call without a trace.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from nanodot.core.config import Config
from nanodot.core.model_usage import ModelUsage
from nanodot.native.providers import configured_provider
from nanodot.native.providers.anthropic import AnthropicProvider
from nanodot.native.providers.openai_compat import APIInferenceProvider
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.inference import ProviderError, StateChange

CHANGE = StateChange(kind="checks-failed", summary="ci failing", head_sha="s")


class _Response(io.BytesIO):
    def __init__(self, body: dict) -> None:
        super().__init__(json.dumps(body).encode())
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def openai_body(text: str, usage: dict | None = None) -> dict:
    body = {"choices": [{"message": {"content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def patch_transport(monkeypatch, module: str, responder):
    monkeypatch.setattr(
        f"nanodot.native.providers.{module}.authenticated_urlopen", responder
    )


# -- the store --------------------------------------------------------------------


def test_usage_accumulates_per_provider_and_day(home: Path) -> None:
    usage = ModelUsage(path=home / "nanodot.db", tzname="UTC")
    usage.record("openai-compat", input_tokens=100, output_tokens=20)
    usage.record("openai-compat", input_tokens=50, output_tokens=10)
    usage.record("anthropic", input_tokens=7, output_tokens=3)
    flat = {r["provider"]: r for r in usage.summary()}
    assert flat["openai-compat"]["calls"] == 2
    assert flat["openai-compat"]["input_tokens"] == 150
    assert flat["openai-compat"]["output_tokens"] == 30
    assert flat["anthropic"]["calls"] == 1


def test_day_rollover_resets_counters(home: Path) -> None:
    usage = ModelUsage(path=home / "nanodot.db", tzname="UTC")
    day1 = 1_700_000_000.0
    midnight = 1_700_006_400.0  # next UTC day
    usage.record("glm", now=day1)
    usage.record("glm", now=day1)
    assert usage.calls_today("glm", now=day1) == 2
    assert usage.calls_today("glm", now=midnight) == 0


# -- adapters record what responses report ------------------------------------------


def test_openai_adapter_records_token_usage(home: Path, monkeypatch) -> None:
    usage = ModelUsage(path=home / "nanodot.db", tzname="UTC")
    patch_transport(
        monkeypatch, "openai_compat",
        lambda request, timeout=None: _Response(
            openai_body("ok", {"prompt_tokens": 120, "completion_tokens": 30})
        ),
    )
    provider = APIInferenceProvider(api_key="sk", name="openai-compat", usage=usage)
    assert provider.summarize(CHANGE) == "ok"
    rows = provider_rows(usage)
    assert rows["openai-compat"]["calls"] == 1
    assert rows["openai-compat"]["input_tokens"] == 120
    assert rows["openai-compat"]["output_tokens"] == 30


def test_anthropic_adapter_records_token_usage(home: Path, monkeypatch) -> None:
    usage = ModelUsage(path=home / "nanodot.db", tzname="UTC")
    patch_transport(
        monkeypatch, "anthropic",
        lambda request, timeout=None: _Response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 9, "output_tokens": 4},
            }
        ),
    )
    provider = AnthropicProvider(api_key="sk", name="anthropic", usage=usage)
    assert provider.summarize(CHANGE) == "ok"
    rows = provider_rows(usage)
    assert rows["anthropic"]["input_tokens"] == 9
    assert rows["anthropic"]["output_tokens"] == 4


def provider_rows(usage: ModelUsage) -> dict:
    return {r["provider"]: r for r in usage.summary()}


# -- the opt-in budget: fail closed, observable, before any socket ------------------


def test_budget_exceeded_raises_before_transport(home: Path, monkeypatch) -> None:
    usage = ModelUsage(path=home / "nanodot.db", tzname="UTC")
    for _ in range(3):
        usage.record("openai-compat")
    patch_transport(
        monkeypatch, "openai_compat",
        lambda request, timeout=None: pytest.fail("no socket use allowed"),
    )
    provider = APIInferenceProvider(
        api_key="sk", name="openai-compat", usage=usage, daily_limit=3
    )
    with pytest.raises(ProviderError, match="daily limit reached"):
        provider.summarize(CHANGE)
    with pytest.raises(ProviderError, match="daily limit reached"):
        provider.parse_intent("watch o/r#1")


def test_budget_counts_calls_not_tokens(home: Path, monkeypatch) -> None:
    usage = ModelUsage(path=home / "nanodot.db", tzname="UTC")
    calls = {"n": 0}

    def responder(request, timeout=None):
        calls["n"] += 1
        return _Response(
            openai_body("ok", {"prompt_tokens": 10**9, "completion_tokens": 10**9})
        )

    patch_transport(monkeypatch, "openai_compat", responder)
    provider = APIInferenceProvider(
        api_key="sk", name="openai-compat", usage=usage, daily_limit=2
    )
    assert provider.summarize(CHANGE) == "ok"
    assert provider.summarize(CHANGE) == "ok"
    with pytest.raises(ProviderError, match="daily limit reached"):
        provider.summarize(CHANGE)
    assert calls["n"] == 2  # exactly the budget left the host


def test_configured_provider_reads_limit_from_config(home: Path, monkeypatch) -> None:
    FileSecretStore().set("api-key", "sk")
    Config().set("model-name", "m1")
    Config().set("model-base-url", "https://model.test/v1")
    Config().set("model-daily-limit", 5)
    from nanodot.native import providers

    providers._usage_store = ModelUsage(tzname="UTC")  # isolated home
    monkeypatch.setattr(providers, "shared_usage_store", lambda: providers._usage_store)
    from nanodot.native.providers.retry import RetryingProvider

    provider = configured_provider()
    assert isinstance(provider, RetryingProvider)
    adapter = provider._inner
    assert isinstance(adapter, APIInferenceProvider)
    assert adapter._daily_limit == 5
    assert adapter._usage is providers._usage_store


def test_limit_validation_at_set_time(home: Path) -> None:
    with pytest.raises(ValueError):
        Config().set("model-daily-limit", "many")
    with pytest.raises(ValueError):
        Config().set("model-daily-limit", -1)
    Config().set("model-daily-limit", "0")  # string digits normalize; 0 = off
    assert Config().get("model-daily-limit") == 0


# -- the CLI surface ----------------------------------------------------------------


def test_model_usage_command_rends_rows(home: Path, capsys) -> None:
    from nanodot.cli import main
    from nanodot.native import providers

    providers._usage_store = ModelUsage(tzname="UTC")
    providers._usage_store.record("glm", input_tokens=10, output_tokens=5)
    assert main(["model", "usage"]) == 0
    out = capsys.readouterr().out
    assert "glm" in out and "calls=1" in out and "10 in / 5 out" in out
    assert "no daily limit" in out


def test_model_usage_command_empty(home: Path, capsys) -> None:
    from nanodot.cli import main
    from nanodot.native import providers

    providers._usage_store = ModelUsage(tzname="UTC")
    assert main(["model", "usage"]) == 0
    assert "no calls recorded" in capsys.readouterr().out

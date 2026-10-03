"""Provider registry + first-wave adapter contract tests (issue #46 P0/P1).

All offline: every transport is faked. The invariants under test are the
RFC's: whitelist payloads regardless of protocol, credentials only in
headers to the pinned destination, ProviderError on every failure, and
dual-read configuration that keeps legacy installs working unchanged.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from nanodot.core.config import Config
from nanodot.native.providers import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    configured_provider,
    provider_names,
)
from nanodot.native.providers.anthropic import AnthropicProvider
from nanodot.native.providers.openai_compat import APIInferenceProvider
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.inference import ProviderError, StateChange

CHANGE = StateChange(kind="checks-failed", summary="ci failing", head_sha="s")


class _Response(io.BytesIO):
    def __init__(self, body: dict | str, status: int = 200) -> None:
        raw = body if isinstance(body, str) else json.dumps(body)
        super().__init__(raw.encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def anthropic_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# -- the registry ----------------------------------------------------------------


def test_registry_names_and_presets() -> None:
    assert set(provider_names()) >= {
        "openai-compat", "anthropic", "glm", "deepseek",
    }
    assert PROVIDERS["glm"][1] == "https://open.bigmodel.cn/api/paas/v4"
    assert PROVIDERS["deepseek"][1] == "https://api.deepseek.com"
    assert PROVIDERS["anthropic"][1] == "https://api.anthropic.com"


def test_absent_configuration_means_no_provider(home: Path) -> None:
    assert configured_provider() is None
    FileSecretStore().set("api-key", "sk-legacy")
    assert configured_provider() is None  # model still missing: no provider


def test_unknown_provider_name_degrades_to_none(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"model-provider": "nope"}))
    FileSecretStore().set("api-key:nope", "sk-x")
    assert configured_provider() is None


def test_legacy_keys_build_openai_compat_unchanged(home: Path) -> None:
    FileSecretStore().set("api-key", "sk-legacy")
    Config().set("model-base-url", "https://model.test/v1")
    Config().set("model-name", "m1")
    provider = configured_provider()
    assert isinstance(provider, APIInferenceProvider)
    assert provider._base_url == "https://model.test/v1"
    assert provider._model == "m1"


def test_namespaced_keys_win_and_use_presets(home: Path) -> None:
    FileSecretStore().set("api-key", "sk-legacy-ignored")
    FileSecretStore().set("api-key:glm", "sk-glm")
    Config().set("model-provider", "glm")
    Config().set("provider.glm.model", "glm-4.7")
    provider = configured_provider()
    assert isinstance(provider, APIInferenceProvider)
    assert provider._base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert provider._model == "glm-4.7"


def test_custom_base_url_overrides_preset(home: Path) -> None:
    FileSecretStore().set("api-key:glm", "sk-glm")
    Config().set("model-provider", "glm")
    Config().set("provider.glm.model", "glm-4.7")
    Config().set("provider.glm.base-url", "https://my-vllm.internal/v1")
    assert configured_provider()._base_url == "https://my-vllm.internal/v1"


def test_invalid_url_is_observable_not_silent(home: Path) -> None:
    FileSecretStore().set("api-key", "sk-legacy")
    Config().set("model-base-url", "bad url")
    Config().set("model-name", "m1")
    provider = configured_provider()
    assert provider is not None  # present-but-broken, not "no model"
    with pytest.raises(ProviderError, match="misconfigured"):
        provider.summarize(CHANGE)


# -- namespaced secrets route to the store -----------------------------------------


def test_namespaced_api_key_is_a_secret_name() -> None:
    from nanodot.core.redaction import is_secret_name

    assert is_secret_name("api-key:openai-compat")
    assert is_secret_name("api-key:glm")
    assert not is_secret_name("api-key:")


def test_cli_rejects_unknown_provider(home: Path, capsys) -> None:
    from nanodot.cli import main

    assert main(["config", "set", "model-provider", "nope"]) == 1
    assert "model-provider must be one of" in capsys.readouterr().err
    assert main(["config", "set", "model-provider", "anthropic"]) == 0


# -- the anthropic adapter: different protocol, same invariants --------------------


def test_anthropic_wire_shape_and_whitelist(home: Path, monkeypatch) -> None:
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _Response(anthropic_text("a model summary"))

    monkeypatch.setattr(
        "nanodot.native.providers.anthropic.authenticated_urlopen", fake_urlopen
    )
    provider = AnthropicProvider(api_key="sk-ant", model="claude-sonnet-4-5")

    assert provider.summarize(CHANGE) == "a model summary"
    (request,) = seen
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.headers["X-api-key"] == "sk-ant"
    assert request.headers["Anthropic-version"] == "2023-06-01"
    assert "Authorization" not in request.headers  # no bearer confusion
    body = json.loads(request.data.decode())
    assert set(body) == {"model", "max_tokens", "system", "messages"}
    user_content = body["messages"][0]["content"]
    payload = json.loads(user_content)
    assert set(payload) <= {
        "kind", "summary", "head_sha", "pr_state", "url", "checks",
    }  # the EgressGuard whitelist, unchanged by the protocol


def test_anthropic_parse_intent_with_fence(monkeypatch) -> None:
    monkeypatch.setattr(
        "nanodot.native.providers.anthropic.authenticated_urlopen",
        lambda request, timeout=None: _Response(
            anthropic_text('```json\n{"target": "o/r#2", "purpose": "watch"}\n```')
        ),
    )
    draft = AnthropicProvider(api_key="sk-ant").parse_intent("watch o/r#2")
    assert draft.target == "o/r#2" and draft.purpose == "watch"


@pytest.mark.parametrize(
    "failure",
    [
        HTTPError("u", 429, "limited", {}, io.BytesIO(b"{}")),
        HTTPError("u", 500, "boom", {}, io.BytesIO(b"{}")),
        OSError("offline"),
    ],
)
def test_anthropic_failures_are_provider_errors(monkeypatch, failure) -> None:
    def fake_urlopen(request, timeout=None):
        raise failure

    monkeypatch.setattr(
        "nanodot.native.providers.anthropic.authenticated_urlopen", fake_urlopen
    )
    with pytest.raises(ProviderError):
        AnthropicProvider(api_key="sk-ant").summarize(CHANGE)


def test_anthropic_scrubs_its_own_key_from_errors(home: Path, monkeypatch) -> None:
    def fake_urlopen(request, timeout=None):
        raise HTTPError(
            "u", 400, "bad", {}, io.BytesIO(b"sk-ant leaked in error body")
        )

    monkeypatch.setattr(
        "nanodot.native.providers.anthropic.authenticated_urlopen", fake_urlopen
    )
    with pytest.raises(ProviderError) as excinfo:
        AnthropicProvider(api_key="sk-ant-secret-value").summarize(CHANGE)
    assert "sk-ant-secret-value" not in str(excinfo.value)

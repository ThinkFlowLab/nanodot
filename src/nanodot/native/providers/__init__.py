"""The provider registry — how a model provider is selected and built.

One provider is selected by name (``model-provider``, default
``openai-compat``). Each registry entry is a preset: a factory plus a
default base URL, so a provider with a namespaced key and model needs no
base-URL configuration at all. Resolution is dual-read for migration
continuity: namespaced settings (``provider.<name>.*``, secret
``api-key:<name>``) win, legacy flat keys (``model-base-url``,
``model-name``, secret ``api-key``) keep working for ``openai-compat``
and are never migrated or rewritten.

An unknown provider name — however it got into the config — resolves to
no provider at all: the watch degrades to raw messages, fail closed.
"""

from __future__ import annotations

from typing import Callable

from nanodot.core.config import Config
from nanodot.core.redaction import Redactor
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.inference import InferenceProvider, ProviderError, TaskDraft

DEFAULT_PROVIDER = "openai-compat"
DEFAULT_TIMEOUT = 5.0
PROVIDER_CONFIG_PREFIX = "provider."


def _openai_compat_factory(
    api_key: str, base_url: str, model: str, timeout: float,
    redactor: Redactor, name: str = "openai-compat",
    usage=None, daily_limit: int | None = None,
) -> InferenceProvider:
    from nanodot.native.providers.openai_compat import APIInferenceProvider

    return APIInferenceProvider(
        api_key=api_key, base_url=base_url, model=model,
        redactor=redactor, timeout=timeout, name=name,
        usage=usage, daily_limit=daily_limit,
    )


def _anthropic_factory(
    api_key: str, base_url: str, model: str, timeout: float,
    redactor: Redactor, name: str = "anthropic",
    usage=None, daily_limit: int | None = None,
) -> InferenceProvider:
    from nanodot.native.providers.anthropic import AnthropicProvider

    return AnthropicProvider(
        api_key=api_key, base_url=base_url, model=model,
        redactor=redactor, timeout=timeout, name=name,
        usage=usage, daily_limit=daily_limit,
    )


_usage_store = None


def shared_usage_store():
    """One process-wide usage store; memoized so repeated
    configured_provider() calls do not pile up connections."""
    global _usage_store
    if _usage_store is None:
        from nanodot.core.model_usage import ModelUsage

        _usage_store = ModelUsage()
    return _usage_store


# name -> (factory, preset base URL)
PROVIDERS: dict[str, tuple[Callable[..., InferenceProvider], str]] = {
    "openai-compat": (_openai_compat_factory, "https://api.openai.com/v1"),
    "anthropic": (_anthropic_factory, "https://api.anthropic.com"),
    "glm": (_openai_compat_factory, "https://open.bigmodel.cn/api/paas/v4"),
    "deepseek": (_openai_compat_factory, "https://api.deepseek.com"),
}


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(PROVIDERS))


def _validate_base_url(url: str) -> str:
    # Destination pinning: https only, a host, no fragments — validated once
    # at construction so "what host did we send to" is a config fact.
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.fragment:
        raise ValueError(f"provider base URL must be https without fragment: {url!r}")
    return url.rstrip("/")


class _MisconfiguredProvider(InferenceProvider):
    """A present-but-broken configuration: every call names the problem.

    Absent configuration means no provider (silent degrade); invalid
    configuration must stay observable per call, never masquerade as
    "no model configured".
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def summarize(self, change) -> str:
        raise ProviderError(f"provider misconfigured: {self._reason}")

    def parse_intent(self, text: str) -> "TaskDraft":
        raise ProviderError(f"provider misconfigured: {self._reason}")


def configured_provider() -> InferenceProvider | None:
    """A provider if and only if the name, key, and model all resolve."""
    config = Config()
    name = str(config.get("model-provider", DEFAULT_PROVIDER))
    entry = PROVIDERS.get(name)
    if entry is None:
        return None  # unknown name: no provider, degrade — never guess
    factory, preset_base = entry

    namespaced = lambda key: config.get(f"{PROVIDER_CONFIG_PREFIX}{name}.{key}")
    legacy = (name == DEFAULT_PROVIDER)

    base_url = namespaced("base-url")
    if base_url is None and legacy:
        base_url = config.get("model-base-url")
    if base_url is None:
        base_url = preset_base

    model = namespaced("model")
    if model is None and legacy:
        model = config.get("model-name")

    timeout = namespaced("timeout")
    try:
        timeout = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        return _MisconfiguredProvider("provider timeout must be a number")
    if timeout <= 0:
        return _MisconfiguredProvider("provider timeout must be positive")

    secrets = FileSecretStore()
    api_key = secrets.get(f"api-key:{name}")
    if api_key is None and legacy:
        api_key = secrets.get("api-key")

    if not (api_key and base_url and model):
        return None
    try:
        base_url = _validate_base_url(str(base_url))
    except ValueError as error:
        return _MisconfiguredProvider(str(error))
    limit = config.get("model-daily-limit")
    from nanodot.native.providers.retry import RetryingProvider

    return RetryingProvider(factory(
        api_key=api_key, base_url=base_url, model=str(model),
        timeout=timeout, redactor=Redactor(secrets), name=name,
        usage=shared_usage_store(),
        daily_limit=int(limit) if limit is not None else None,
    ))

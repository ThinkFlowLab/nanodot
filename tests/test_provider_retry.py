"""Bounded retry with Retry-After semantics (issue #46 P2).

Offline: the inner provider is a scriptable fake, the sleep is captured
— no real waiting, no network. Policy: retry only transport-class
failures and retryable statuses (429/5xx), honor Retry-After capped by
the ceiling, exponential backoff with jitter otherwise, bounded attempts,
last error re-raised.
"""

from __future__ import annotations

import pytest

from nanodot.native.providers.retry import (
    DEFAULT_MAX_DELAY,
    RETRYABLE_STATUSES,
    RetryingProvider,
    retry_after_seconds,
)
from nanodot.ports.inference import ProviderError, StateChange, TaskDraft

CHANGE = StateChange(kind="k", summary="s")


class Flaky:
    """Fails N times with given errors, then succeeds."""

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.calls = 0

    def summarize(self, change):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"

    def parse_intent(self, text):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return TaskDraft(target="o/r#1", purpose="p")


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def make(inner, attempts=3, base=0.5, ceiling=8.0, rng=lambda: 0.0):
    recorder = Recorder()
    provider = RetryingProvider(
        inner, attempts=attempts, base_delay=base, max_delay=ceiling,
        sleep=recorder, rng=rng,
    )
    return provider, recorder


def throttle(retry_after: float | None = None) -> ProviderError:
    return ProviderError(
        "429", status=429, retryable=True, retry_after=retry_after,
    )


# -- what retries, what does not ---------------------------------------------------


def test_retry_after_is_honored_exactly() -> None:
    provider, recorder = make(Flaky([throttle(2.0)]))
    assert provider.summarize(CHANGE) == "ok"
    assert recorder.sleeps == [2.0]  # Retry-After wins over backoff


def test_retry_after_is_capped_by_ceiling() -> None:
    provider, recorder = make(Flaky([throttle(120.0)]), ceiling=8.0)
    assert provider.summarize(CHANGE) == "ok"
    assert recorder.sleeps == [8.0]


def test_exponential_backoff_without_retry_after() -> None:
    provider, recorder = make(Flaky([throttle(), throttle()]))
    assert provider.summarize(CHANGE) == "ok"
    assert recorder.sleeps == [0.5, 1.0]


def test_backoff_is_capped() -> None:
    errors = [throttle() for _ in range(4)]
    provider, recorder = make(Flaky(errors), attempts=5, base=4.0, ceiling=8.0)
    assert provider.summarize(CHANGE) == "ok"
    assert recorder.sleeps == [4.0, 8.0, 8.0, 8.0]


def test_jitter_bounds_the_delay() -> None:
    provider, recorder = make(
        Flaky([throttle()]), rng=lambda: 1.0  # max jitter
    )
    assert provider.summarize(CHANGE) == "ok"
    (delay,) = recorder.sleeps
    assert delay == 0.5 * 1.25  # delay + 25% jitter at the extreme draw


@pytest.mark.parametrize("error", [
    ProviderError("400 refused", status=400),
    ProviderError("empty summary"),
    ProviderError("misconfigured: bad url"),
])
def test_permanent_failures_do_not_retry(error) -> None:
    inner = Flaky([error])
    provider, recorder = make(inner)
    with pytest.raises(ProviderError):
        provider.summarize(CHANGE)
    assert inner.calls == 1 and recorder.sleeps == []


def test_transport_errors_retry() -> None:
    provider, recorder = make(Flaky([ProviderError("offline", retryable=True)]))
    assert provider.summarize(CHANGE) == "ok"
    assert recorder.sleeps == [0.5]


def test_exhaustion_reraises_the_last_error() -> None:
    inner = Flaky([throttle(), throttle(), throttle(), throttle()])
    provider, recorder = make(inner, attempts=3)
    with pytest.raises(ProviderError) as excinfo:
        provider.summarize(CHANGE)
    assert excinfo.value.status == 429
    assert inner.calls == 3
    assert recorder.sleeps == [0.5, 1.0]


def test_parse_intent_retries_too() -> None:
    provider, recorder = make(Flaky([throttle(1.0)]))
    assert provider.parse_intent("watch o/r#1").target == "o/r#1"
    assert recorder.sleeps == [1.0]


def test_retryable_status_table() -> None:
    assert RETRYABLE_STATUSES == {429, 500, 502, 503, 504}


def test_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RetryingProvider(Flaky([]), attempts=0)


# -- the header parser ---------------------------------------------------------------


class Headers:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get(self, name, default=None):
        return self.value if name == "Retry-After" else default


def test_retry_after_parsing() -> None:
    assert retry_after_seconds(Headers("3")) == 3.0
    assert retry_after_seconds(Headers("  7 ")) == 7.0
    assert retry_after_seconds(Headers("Wed, 21 Oct 2026 07:28:00 GMT")) is None
    assert retry_after_seconds(Headers(None)) is None
    assert retry_after_seconds(None) is None


# -- end to end through configured_provider -------------------------------------------


def test_configured_provider_retries_through_the_wrapper(
    home, monkeypatch
) -> None:
    from nanodot.core.config import Config
    from nanodot.native.providers import configured_provider
    from nanodot.native.secrets_file import FileSecretStore

    FileSecretStore().set("api-key", "sk")
    Config().set("model-base-url", "https://model.test/v1")
    Config().set("model-name", "m1")

    import io
    import json
    import urllib.error

    state = {"calls": 0}

    def urlopen(request, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "slow down",
                {"Retry-After": "0"}, io.BytesIO(b"{}"),
            )
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": "recovered"}}]}
        ).encode())

    monkeypatch.setattr(
        "nanodot.native.providers.openai_compat.authenticated_urlopen", urlopen
    )
    provider = configured_provider()
    # Retry-After: 0 means no real sleeping; the wrapper recovered transparently.
    assert provider.summarize(CHANGE) == "recovered"
    assert state["calls"] == 2

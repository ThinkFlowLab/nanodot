"""Bounded retry with Retry-After semantics for every provider.

Retry policy is uniform and lives outside the protocol adapters: they map
failures to structured ProviderError fields (status, retryable,
retry_after), and ``RetryingProvider`` — wrapped around whatever the
factory builds — decides. Bounded attempts, exponential backoff capped by
a ceiling, Retry-After honored when the provider supplies one, jitter so
throttled clients do not synchronize. Exhaustion re-raises the last
ProviderError: the degrade-never-crash contract is unchanged, and the
scheduler's summary budget already discards responses that arrive too
late, so a retrying call can never hold up the watch loop.
"""

from __future__ import annotations

import random
import time
from typing import Callable

from nanodot.ports.inference import InferenceProvider, ProviderError, StateChange, TaskDraft

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0
JITTER_FRACTION = 0.25


def retry_after_seconds(headers) -> float | None:
    """Parse a Retry-After header: seconds form only, bounded by the cap.

    The HTTP-date form is ignored (treated as absent) — an absolute-date
    wait beyond the ceiling is useless to a bounded retry.
    """
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None or not value.strip().isdigit():
        return None
    return float(value.strip())


class RetryingProvider(InferenceProvider):
    def __init__(
        self,
        inner: InferenceProvider,
        attempts: int = DEFAULT_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self._inner = inner
        self._attempts = attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._rng = rng or random.random

    def _delay_for(self, failure: ProviderError, attempt: int) -> float:
        if failure.retry_after is not None:
            delay = min(failure.retry_after, self._max_delay)
        else:
            delay = min(self._base_delay * (2 ** attempt), self._max_delay)
        jitter = self._rng() * JITTER_FRACTION * delay
        return min(delay + jitter, self._max_delay * (1 + JITTER_FRACTION))

    def _call(self, method: str, *args):
        last: ProviderError | None = None
        for attempt in range(self._attempts):
            try:
                return getattr(self._inner, method)(*args)
            except ProviderError as error:
                last = error
                retryable = error.retryable or (
                    error.status is not None and error.status in RETRYABLE_STATUSES
                )
                if not retryable or attempt == self._attempts - 1:
                    raise
                self._sleep(self._delay_for(error, attempt))
        raise last  # unreachable; for type checkers

    def summarize(self, change: StateChange) -> str:
        return self._call("summarize", change)

    def parse_intent(self, text: str) -> TaskDraft:
        return self._call("parse_intent", text)

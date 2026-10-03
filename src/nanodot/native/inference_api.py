"""Compatibility shim — the provider moved to ``native.providers``.

Existing importers (runner wiring, tests) keep working; the non-GET
invariant test still names this module. New code imports from
``nanodot.native.providers``.
"""

from __future__ import annotations

from nanodot.native.providers import configured_provider
from nanodot.native.providers.openai_compat import (
    API_KEY_SECRET,
    DEFAULT_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT,
    APIInferenceProvider,
)

__all__ = [
    "APIInferenceProvider",
    "API_KEY_SECRET",
    "DEFAULT_BASE_URL",
    "DEFAULT_REQUEST_TIMEOUT",
    "configured_provider",
]

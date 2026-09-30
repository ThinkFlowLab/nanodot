"""API-backed inference provider (OpenAI-compatible chat completions).

The API key comes from the secret store; the request body is built by the
EgressGuard whitelist and is the only data that leaves the host (besides
the Authorization header). All failures raise ProviderError — callers
degrade, never crash.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from nanodot.core.config import Config
from nanodot.core.egress import EgressGuard
from nanodot.core.redaction import Redactor
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.inference import (
    InferenceProvider,
    ProviderError,
    StateChange,
    TaskDraft,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
API_KEY_SECRET = "api-key"


def configured_provider() -> "APIInferenceProvider | None":
    """A provider if and only if a key, base URL, and model are set."""
    secrets = FileSecretStore()
    config = Config()
    key = secrets.get(API_KEY_SECRET)
    base_url = config.get("model-base-url")
    model = config.get("model-name")
    if not (key and base_url and model):
        return None
    return APIInferenceProvider(
        api_key=key, base_url=str(base_url), model=str(model)
    )


class APIInferenceProvider(InferenceProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "gpt-4o-mini",
        redactor: Redactor | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._guard = EgressGuard(redactor)

    # -- transport ---------------------------------------------------------

    def _chat(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode()
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:200]
            raise ProviderError(f"model API error {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise ProviderError(f"model API unreachable: {error}") from error
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"unexpected model API shape: {error}") from error

    # -- port ---------------------------------------------------------------

    def summarize(self, change: StateChange) -> str:
        payload = self._guard.summarize_request(change)
        content = self._chat(
            system="You summarize CI state changes for a developer, in one "
            "or two plain sentences. Use only the provided data.",
            user=json.dumps(payload),
        )
        if not content.strip():
            raise ProviderError("model returned an empty summary")
        return content.strip()

    def parse_intent(self, text: str) -> TaskDraft:
        payload = self._guard.intent_request(text)
        content = self._chat(
            system="You convert a user request into JSON with exactly the "
            'keys "target" (owner/repo#number) and "purpose" (short). '
            "Reply with JSON only.",
            user=json.dumps(payload),
        )
        try:
            parsed = json.loads(content)
            return TaskDraft(
                target=str(parsed["target"]),
                purpose=str(parsed["purpose"]),
                raw=content,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ProviderError(f"model intent not parseable: {error}") from error

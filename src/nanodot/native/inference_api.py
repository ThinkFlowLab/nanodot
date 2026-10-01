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
from nanodot.native.http import authenticated_urlopen
from nanodot.ports.inference import (
    InferenceProvider,
    ProviderError,
    StateChange,
    TaskDraft,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
API_KEY_SECRET = "api-key"
DEFAULT_REQUEST_TIMEOUT = 5.0


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
        api_key=key, base_url=str(base_url), model=str(model),
        redactor=Redactor(secrets),
    )


class APIInferenceProvider(InferenceProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "gpt-4o-mini",
        redactor: Redactor | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if timeout <= 0:
            raise ValueError("provider timeout must be positive")
        self._timeout = timeout
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._redactor = redactor
        self._guard = EgressGuard(scrubber=self._scrub)

    # -- transport ---------------------------------------------------------

    def _chat(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self._scrub(self._model),
                "messages": [
                    {"role": "system", "content": self._scrub(system)},
                    {"role": "user", "content": self._scrub(user)},
                ],
            }
        ).encode()
        try:
            request = urllib.request.Request(
                f"{self._base_url}/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            with authenticated_urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = self._scrub(error.read().decode(errors="replace"))[:200]
            raise ProviderError(f"model API error {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise ProviderError(self._scrub(f"model API unreachable: {error}")) from error
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not text")
            return self._scrub(content)
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"unexpected model API shape: {error}") from error

    def _scrub(self, text: str) -> str:
        # A directly constructed provider still protects its own API key.
        text = self._redactor.scrub(text) if self._redactor else text
        return text.replace(self._api_key, "***") if self._api_key else text

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
            structured = content.strip()
            if structured.startswith("```"):
                lines = structured.splitlines()
                if len(lines) >= 3 and lines[0].lower() in {"```", "```json"} and lines[-1] == "```":
                    structured = "\n".join(lines[1:-1])
            parsed = json.loads(structured)
            if not isinstance(parsed, dict) or not all(
                isinstance(parsed.get(field), str) and parsed[field].strip()
                for field in ("target", "purpose")
            ):
                raise ValueError("target and purpose must be nonempty strings")
            clean = {field: self._scrub(parsed[field]) for field in ("target", "purpose")}
            return TaskDraft(
                target=clean["target"],
                purpose=clean["purpose"],
                raw=json.dumps(clean),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(f"model intent not parseable: {error}") from error

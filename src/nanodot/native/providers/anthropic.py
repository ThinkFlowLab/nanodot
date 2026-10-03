"""Anthropic Messages API provider.

The one non-chat-completions protocol in the first wave: it proves the
provider architecture handles a different wire shape (x-api-key header,
anthropic-version, system-as-top-level, content-block responses) while
the EgressGuard payload — what may leave — is identical to every other
provider. All failures raise ProviderError; callers degrade, never crash.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from nanodot.core.egress import EgressGuard
from nanodot.core.redaction import Redactor
from nanodot.native.http import authenticated_urlopen
from nanodot.ports.inference import (
    InferenceProvider,
    ProviderError,
    StateChange,
    TaskDraft,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 512
DEFAULT_REQUEST_TIMEOUT = 5.0


def _strip_code_fence(text: str) -> str:
    structured = text.strip()
    if structured.startswith("```"):
        lines = structured.splitlines()
        if (
            len(lines) >= 3
            and lines[0].lower() in {"```", "```json"}
            and lines[-1] == "```"
        ):
            structured = "\n".join(lines[1:-1])
    return structured


class AnthropicProvider(InferenceProvider):
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "claude-sonnet-4-5",
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

    def _scrub(self, text: str) -> str:
        text = self._redactor.scrub(text) if self._redactor else text
        return text.replace(self._api_key, "***") if self._api_key else text

    def _messages(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self._scrub(self._model),
                "max_tokens": MAX_TOKENS,
                "system": self._scrub(system),
                "messages": [{"role": "user", "content": self._scrub(user)}],
            }
        ).encode()
        try:
            request = urllib.request.Request(
                f"{self._base_url}/v1/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                method="POST",
            )
            with authenticated_urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = self._scrub(error.read().decode(errors="replace"))[:200]
            raise ProviderError(
                f"model API error {error.code}: {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                http.client.HTTPException) as error:
            raise ProviderError(
                self._scrub(f"model API unreachable: {error}")
            ) from error
        try:
            blocks = payload["content"]
            text = next(block["text"] for block in blocks if block.get("type") == "text")
            if not isinstance(text, str):
                raise TypeError("message content is not text")
            return self._scrub(text)
        except (StopIteration, KeyError, IndexError, TypeError) as error:
            raise ProviderError(f"unexpected model API shape: {error}") from error

    # -- port ---------------------------------------------------------------

    def summarize(self, change: StateChange) -> str:
        payload = self._guard.summarize_request(change)
        content = self._messages(
            system="You summarize CI state changes for a developer, in one "
            "or two plain sentences. Use only the provided data.",
            user=json.dumps(payload),
        )
        if not content.strip():
            raise ProviderError("model returned an empty summary")
        return content.strip()

    def parse_intent(self, text: str) -> TaskDraft:
        payload = self._guard.intent_request(text)
        content = self._messages(
            system="You convert a user request into JSON with exactly the "
            'keys "target" (owner/repo#number) and "purpose" (short). '
            "Reply with JSON only.",
            user=json.dumps(payload),
        )
        try:
            parsed = json.loads(_strip_code_fence(content))
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

"""Native GitHub writer — the single non-GET transport in nanodot.

Issues an authorized write only through a ``WriteCapability`` whose
content hash matches the exact payload bytes, only with the separated
write credential (never the read token), and never along a redirect —
the same no-redirect policy as reads (``native/http.py``). Without a
configured credential every call raises before any socket is opened.

The action table maps an approved action to its verb and API path; it is
empty until the first write action lands (#54). Tests may pass their own
table so the transport is exercisable offline without inventing actions.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

from nanodot.core.tasks import PRTarget
from nanodot.native.http import authenticated_urlopen
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github_writer import (
    WriteActionUnknown,
    WriteCapability,
    WriteContentMismatch,
    WriteCredentialMissing,
    WriteRejectedError,
    WriteResult,
    WriteTransportError,
)

API_BASE = "https://api.github.com"
WRITE_TOKEN_SECRET = "github-write-token"

# action -> (verb, path template); the first real entries land with #54.
ActionTable = dict[str, tuple[str, str]]


def _resolve(template: str, target: PRTarget) -> str:
    try:
        path = template.format(
            owner=target.owner, repo=target.repo, number=target.number
        )
    except (KeyError, IndexError, ValueError) as error:
        raise WriteActionUnknown(f"malformed action path template: {error}") from error
    if ".." in path or "://" in path or not path.startswith("/"):
        raise WriteActionUnknown("action path escapes the pinned destination")
    return path


class GitHubWriter:
    def __init__(
        self,
        token: str | None = None,
        base_url: str = API_BASE,
        actions: ActionTable | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._actions = actions if actions is not None else {}
        self._timeout = timeout

    def _write_token(self) -> str:
        if self._token is not None:
            return self._token
        token = FileSecretStore().get(WRITE_TOKEN_SECRET)
        if not token:
            raise WriteCredentialMissing(
                "no GitHub write token configured "
                "(nanodot config set github-write-token <fine-grained PAT>)"
            )
        return token

    def execute(self, capability: WriteCapability, payload: dict) -> WriteResult:
        endpoint = self._actions.get(capability.action)
        if endpoint is None:
            raise WriteActionUnknown(
                f"no write action {capability.action!r} is implemented"
            )
        verb, template = endpoint
        token = self._write_token()  # before any socket use (fail closed)

        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(body).hexdigest() != capability.content_hash:
            raise WriteContentMismatch(
                "payload does not match the approved content hash; "
                "re-request approval for the changed content"
            )

        target = PRTarget.parse(capability.target)
        path = _resolve(template, target)
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=verb,
        )
        try:
            with authenticated_urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode()
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except ValueError as error:
                    raise WriteTransportError(
                        f"unparseable write response: {error}"
                    ) from error
                return WriteResult(
                    action=capability.action, status=response.status, body=parsed
                )
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:200]
            if 300 <= error.code < 400:
                raise WriteTransportError(
                    f"write redirected ({error.code}); redirects are disabled"
                ) from error
            if 400 <= error.code < 500:
                raise WriteRejectedError(
                    f"GitHub rejected the write ({error.code}): {detail}"
                ) from error
            raise WriteTransportError(
                f"GitHub write error {error.code}: {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise WriteTransportError(f"network error: {error}") from error

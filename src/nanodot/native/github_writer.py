"""Native GitHub comment writer — the only POST in the GitHub surface.

Implements the writer port (docs/design/github-writer.md): verifies the
capability's content hash against the payload before sending, uses only
the separate write token (never the read token's key), and never follows
redirects. Failures are outcomes, not exceptions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from nanodot.core.permissions import WriteCapability, content_digest
from nanodot.core.tasks import PRTarget
from nanodot.native.http import authenticated_urlopen
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github_writer import WriteOutcome

API_BASE = "https://api.github.com"
WRITE_TOKEN_SECRET = "github-write-token"


class GitHubCommentWriter:
    def __init__(self, token: str | None = None, base_url: str = API_BASE) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def execute(self, capability: WriteCapability, body: str) -> WriteOutcome:
        if capability.action != "comment":
            return WriteOutcome(ok=False, error=f"unsupported write action {capability.action!r}")
        if content_digest(body) != capability.content_hash:
            return WriteOutcome(ok=False, error="content hash mismatch; refusing to send")
        try:
            target = PRTarget.parse(capability.target)
        except ValueError:
            return WriteOutcome(ok=False, error=f"invalid write target {capability.target!r}")
        token = self._token if self._token is not None else FileSecretStore().get(WRITE_TOKEN_SECRET)
        if not token:
            return WriteOutcome(
                ok=False,
                error=f"no write token configured (nanodot config set {WRITE_TOKEN_SECRET})",
            )
        url = (
            f"{self._base_url}/repos/{target.owner}/{target.repo}"
            f"/issues/{target.number}/comments"
        )
        request = urllib.request.Request(  # noqa: S310 - fixed https origin
            url,
            data=json.dumps({"body": body}).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with authenticated_urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode())
            return WriteOutcome(ok=True, url=str(result.get("html_url") or ""))
        except urllib.error.HTTPError as error:
            return WriteOutcome(
                ok=False,
                error=f"GitHub rejected the comment (HTTP {error.code})",
            )
        except Exception:
            return WriteOutcome(ok=False, error="comment transport failed")

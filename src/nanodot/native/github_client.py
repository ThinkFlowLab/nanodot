"""Native GitHub client — read-only REST access via a read-only PAT.

Only GET requests are ever issued (enforced by the permissions invariant
test): the client exposes fetch and nothing else.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from nanodot.core.tasks import PRTarget
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github import (
    AuthLostError,
    CheckRun,
    FetchError,
    PRNotFoundError,
    RetryableError,
    Snapshot,
    SnapshotFetcher,
)

API_BASE = "https://api.github.com"
TOKEN_SECRET = "github-token"


class UnexpectedStatusError(FetchError):
    """HTTP status outside the handled ranges — surfaced, never swallowed."""


class GitHubSnapshotFetcher(SnapshotFetcher):
    def __init__(
        self,
        token: str | None = None,
        base_url: str = API_BASE,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _auth_token(self) -> str:
        if self._token is not None:
            return self._token
        token = FileSecretStore().get(TOKEN_SECRET)
        if not token:
            raise AuthLostError(
                "no GitHub token configured (nanodot config set github-token ...)"
            )
        return token

    def _get(self, path: str) -> dict:
        url = f"{self._base_url}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._auth_token()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            body = error.read().decode(errors="replace")
            if error.code == 401:
                raise AuthLostError(
                    f"GitHub rejected the token ({error.code})"
                ) from error
            if error.code == 403:
                if error.headers.get("X-RateLimit-Remaining") == "0":
                    raise RetryableError("GitHub rate limit exceeded") from error
                raise AuthLostError(
                    f"GitHub forbade the request ({error.code})"
                ) from error
            if error.code == 404:
                raise PRNotFoundError(f"not found: {path}") from error
            if error.code == 429 or error.code >= 500:
                raise RetryableError(
                    f"GitHub error {error.code}: {body[:200]}"
                ) from error
            raise UnexpectedStatusError(
                f"GitHub error {error.code}: {body[:200]}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RetryableError(f"network error: {error}") from error

    def fetch(self, target: PRTarget) -> Snapshot:
        repo = f"{target.owner}/{target.repo}"
        pull = self._get(f"/repos/{repo}/pulls/{target.number}")
        head_sha = pull["head"]["sha"]
        pr_state = "merged" if pull.get("merged") else pull["state"]
        # Both requests must succeed before a Snapshot exists — a partial
        # fetch is never returned as a success.
        check_data = self._get(f"/repos/{repo}/commits/{head_sha}/check-runs")
        runs = tuple(
            CheckRun(
                name=run["name"],
                status=run["status"],
                conclusion=run.get("conclusion"),
                sha=head_sha,
            )
            for run in check_data.get("check_runs", [])
        )
        return Snapshot(
            target=target,
            pr_state=pr_state,
            head_sha=head_sha,
            checks=runs,
            fetched_at=time.time(),
            url=f"https://github.com/{repo}/pull/{target.number}",
        )

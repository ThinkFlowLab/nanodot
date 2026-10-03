"""Native GitHub client — read-only REST access via a PAT or anonymously.

Only GET requests are ever issued (enforced by the permissions invariant
test): the client exposes fetch and nothing else. Anonymous mode only has
access to public metadata and never reads or sends a configured token.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from nanodot.core.tasks import PRTarget
from nanodot.native.http import authenticated_urlopen
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.github import (
    AuthLostError,
    CheckRun,
    FetchError,
    PRNotFoundError,
    RetryableError,
    RequiredCheck,
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
        *,
        auth_mode: str = "token",
    ) -> None:
        if auth_mode not in ("token", "anonymous"):
            raise ValueError("github-auth-mode must be token or anonymous")
        self._auth_mode = auth_mode
        self._token = token if auth_mode == "token" else None
        self._base_url = base_url.rstrip("/")

    def _auth_token(self) -> str | None:
        if self._auth_mode == "anonymous":
            return None
        if self._token is not None:
            return self._token
        token = FileSecretStore().get(TOKEN_SECRET)
        if not token:
            raise AuthLostError(
                "no GitHub token configured (nanodot config set github-token ...)"
            )
        return token

    def _get(self, path: str, quota: dict | None = None) -> dict | list:
        url = f"{self._base_url}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # A local secret-store failure (symlinked, unreadable, churning) is
        # a configuration blocker, never a GitHub or network condition.
        try:
            token = self._auth_token()
        except AuthLostError:
            raise
        except (ValueError, OSError) as error:
            raise AuthLostError(f"local secret store unusable: {error}") from error
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url,
            headers=headers,
            method="GET",
        )
        try:
            with authenticated_urlopen(request, timeout=30) as response:
                if quota is not None:
                    # Per-fetch accumulator: the tightest remaining seen.
                    # Advisory transport metadata, never snapshot identity;
                    # a transport without headers simply reports none.
                    try:
                        value = int(
                            getattr(response, "headers", {}).get(
                                "X-RateLimit-Remaining"
                            )
                        )
                    except (TypeError, ValueError, AttributeError):
                        value = None
                    if value is not None:
                        quota["remaining"] = (
                            value if "remaining" not in quota
                            else min(quota["remaining"], value)
                        )
                result = json.loads(response.read().decode())
                if not isinstance(result, (dict, list)):
                    raise RetryableError("malformed GitHub response")
                return result
        except urllib.error.HTTPError as error:
            try:
                body = error.read().decode(errors="replace")
            except (OSError, http.client.HTTPException):
                body = ""  # the status alone still classifies the failure
            if error.code in (301, 308):
                # Redirects are deliberately never followed (the token must
                # not leave the configured host), so a permanent move — e.g.
                # a renamed owner/repo — is a blocker, not an endless retry.
                raise PRNotFoundError(
                    f"GitHub moved this resource permanently (HTTP {error.code}); "
                    "update the watch target"
                ) from error
            if error.code == 401:
                access = "anonymous access" if self._auth_mode == "anonymous" else "the token"
                raise AuthLostError(
                    f"GitHub rejected {access} ({error.code})"
                ) from error
            if error.code == 403:
                if (
                    error.headers.get("X-RateLimit-Remaining") == "0"
                    or error.headers.get("Retry-After") is not None
                    or "rate limit" in body.lower()
                    or "abuse detection" in body.lower()
                ):
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
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RetryableError("malformed GitHub JSON response") from error
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as error:
            # HTTPException (e.g. IncompleteRead on a truncated body) is a
            # routine transport failure and belongs with network trouble.
            raise RetryableError(f"network error: {error}") from error

    def _pages(self, path: str, key: str | None = None, sha: str | None = None,
               quota: dict | None = None) -> list[dict]:
        """Read all pages; never follow server-supplied URLs with our token.

        Counts must stay consistent. An incomplete/changing result is a
        retry, not a snapshot. The budget fails closed on pathological feeds.
        """
        result: list[dict] = []
        expected = None
        seen: set[int] = set()
        separator = "&" if "?" in path else "?"
        for page in range(1, 1001):
            data = self._get(f"{path}{separator}per_page=100&page={page}", quota=quota)
            if key is not None:
                count = data["total_count"]
                if type(count) is not int or count < 0:
                    raise RetryableError("invalid GitHub result count")
                if expected is not None and count != expected:
                    raise RetryableError("GitHub results changed during pagination")
                expected = count
                if sha is not None and data["sha"] != sha:
                    raise RetryableError("GitHub status response has a different SHA")
                batch = data[key]
            else:
                batch = data
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise RetryableError("invalid GitHub result page")
            for row in batch:
                if "id" in row:
                    identity = row["id"]
                    if type(identity) is not int or identity <= 0 or identity in seen:
                        raise RetryableError("duplicate or invalid GitHub result ID")
                    seen.add(identity)
            result.extend(batch)
            if expected is not None and len(result) > expected:
                raise RetryableError("GitHub result count does not match pages")
            if len(batch) < 100 or (expected is not None and len(result) == expected):
                if expected is not None and len(result) != expected:
                    raise RetryableError("incomplete GitHub result pages")
                return result
        raise RetryableError("GitHub pagination budget exceeded; no complete snapshot")

    @staticmethod
    def _requirements(data: dict | None) -> list[RequiredCheck] | None:
        if data is None:
            return []
        contexts = data["contexts"]
        if not isinstance(contexts, list) or any(not isinstance(name, str) or not name for name in contexts):
            return None
        checks = data.get("checks")
        if checks is None:
            # Old summaries omit app-source constraints. Nonempty contexts
            # alone cannot prove that an arbitrary app is allowed.
            return [] if not contexts else None
        if not isinstance(checks, list):
            return None
        required = []
        for check in checks:
            if "app_id" not in check:
                return None
            app_id = check["app_id"]
            if type(app_id) is int and app_id == -1:
                app_id = None
            if app_id is not None and (type(app_id) is not int or app_id <= 0):
                return None
            name = check["context"]
            if not isinstance(name, str) or not name:
                return None
            required.append(RequiredCheck(name, app_id))
        if {name.casefold() for name in contexts} != {check.name.casefold() for check in required}:
            return None
        return required

    def _required_checks(self, repo: str, base_ref: str,
                        quota: dict | None = None) -> tuple[RequiredCheck, ...] | None:
        """Union classic protection and every active inherited ruleset.

        A 404 on protection can mean absent OR hidden metadata: never treat
        it as proof of no requirements. A positive branch summary can prove
        classic protection is off without requiring Administration access.
        """
        branch_path = f"/repos/{repo}/branches/{quote(base_ref, safe='')}"
        try:
            branch = self._get(branch_path, quota=quota)
            if branch.get("protected") is False:
                required = []
            else:
                summary = branch.get("protection", {}).get("required_status_checks")
                if isinstance(summary, dict) and summary.get("enforcement_level") == "off":
                    required = []
                else:
                    required = self._requirements(summary) if summary is not None else None
                    if required is None:
                        protection = self._get(f"{branch_path}/protection", quota=quota)
                        required = self._requirements(protection["required_status_checks"])
                    if required is None:
                        return None
            rules = self._pages(
                f"/repos/{repo}/rules/branches/{quote(base_ref, safe='')}", quota=quota
            )
            # These have no check-context requirements. Other/new rule types
            # (workflows, code scanning, merge queues, deployments) need
            # additional evidence which this head-check watcher cannot prove.
            non_check_rules = {
                "creation", "update", "deletion", "required_linear_history",
                "required_signatures", "pull_request", "non_fast_forward",
                "commit_message_pattern", "commit_author_email_pattern",
                "committer_email_pattern", "branch_name_pattern", "tag_name_pattern",
                "file_path_restriction", "max_file_path_length",
                "file_extension_restriction", "max_file_size",
            }
            for rule in rules:
                if rule["type"] in non_check_rules:
                    continue
                if rule["type"] != "required_status_checks":
                    return None
                entries = rule["parameters"]["required_status_checks"]
                if not isinstance(entries, list):
                    return None
                for entry in entries:
                    name = entry["context"]
                    app_id = entry.get("integration_id")
                    if not isinstance(name, str) or not name or (app_id is not None and (
                        type(app_id) is not int or app_id <= 0
                    )):
                        return None
                    required.append(RequiredCheck(name, app_id))
            return tuple(dict.fromkeys(required))
        except (AuthLostError, PRNotFoundError):
            return None  # metadata inaccessible; observed green checks prove nothing

    def _check_runs(
        self, repo: str, head_sha: str, required: tuple[RequiredCheck, ...] | None,
        quota: dict | None = None,
    ) -> list[CheckRun]:
        runs: list[CheckRun] = []
        # Enumerate suites instead of using commits/{sha}/check-runs, whose
        # results silently omit suites beyond the newest 1,000.
        suites = self._pages(f"/repos/{repo}/commits/{head_sha}/check-suites", "check_suites",
                             quota=quota)
        for suite in suites:
            suite_id = suite["id"]
            if suite["head_sha"] != head_sha:
                raise RetryableError("GitHub check suite has a different SHA")
            app_id = suite["app"]["id"]
            app_slug = suite["app"]["slug"]
            if (type(app_id) is not int or app_id <= 0
                    or not isinstance(app_slug, str) or not app_slug):
                raise RetryableError("missing GitHub check source")
            suite_runs = self._pages(
                f"/repos/{repo}/check-suites/{suite_id}/check-runs?filter=all", "check_runs",
                quota=quota
            )
            unresolved = suite["status"] != "completed"
            relevant = required is not None and any(
                check.app_id in (None, app_id)
                and any(run["name"].casefold() == check.name.casefold() for run in suite_runs)
                for check in required
            )
            if relevant and app_slug == "github-actions":
                # Manual workflow_dispatch runs are not eligible required
                # PR checks. Prove the workflow event and latest rerun state.
                workflows = self._pages(
                    f"/repos/{repo}/actions/runs?check_suite_id={suite_id}", "workflow_runs",
                    quota=quota
                )
                # Reusable workflows (workflow_call) run on the PR head with
                # their own suite and are legitimate required PR checks; runs
                # triggered by other workflows or schedules are not.
                eligible = {"push", "pull_request", "pull_request_review", "pull_request_target", "deployment", "deployment_status", "workflow_call"}
                if len(workflows) != 1:
                    unresolved = True
                else:
                    workflow = workflows[0]
                    unresolved |= (
                        type(workflow["check_suite_id"]) is not int
                        or workflow["check_suite_id"] != suite_id
                        or workflow["head_sha"] != head_sha
                        or workflow["event"] not in eligible
                        or workflow["status"] != "completed"
                    )
            for run in suite_runs:
                # Never relabel a stale/malformed result as the requested SHA.
                if run["head_sha"] != head_sha:
                    raise RetryableError("GitHub check run has a different SHA")
                if (type(run["app"]["id"]) is not int
                        or type(run["check_suite"]["id"]) is not int
                        or run["app"]["id"] != app_id
                        or run["check_suite"]["id"] != suite_id):
                    raise RetryableError("GitHub check run source does not match its suite")
                if not isinstance(run["name"], str) or not run["name"] or not isinstance(run["status"], str):
                    raise RetryableError("malformed GitHub check run")
                conclusion = run["conclusion"]
                if conclusion is not None and not isinstance(conclusion, str):
                    raise RetryableError("malformed GitHub check conclusion")
                runs.append(CheckRun(run["name"], run["status"], conclusion,
                                     run["head_sha"], app_id=app_id,
                                     run_id=run["id"], suite_id=suite_id))
            if unresolved:
                # Known unrelated jobs must not block required contexts just
                # because the same app owns them. An empty queued suite has
                # no names yet, so its potential requirements remain unknown.
                for name in {run["name"] for run in suite_runs} or {""}:
                    runs.append(CheckRun(name, "queued", None, head_sha,
                                         source="check_suite", app_id=app_id, suite_id=suite_id))
        if self._pages(f"/repos/{repo}/commits/{head_sha}/check-suites", "check_suites",
                       quota=quota) != suites:
            raise RetryableError("GitHub check suites changed during snapshot fetch")
        return runs

    def fetch(self, target: PRTarget) -> Snapshot:
        try:
            return self._fetch(target)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            # Bad remote schemas are typed retryable fetch failures, never
            # uncaught adapter errors or partially successful snapshots.
            raise RetryableError("malformed GitHub snapshot data") from error

    def _fetch(self, target: PRTarget) -> Snapshot:
        repo = f"{target.owner}/{target.repo}"
        pull_path = f"/repos/{repo}/pulls/{target.number}"
        quota: dict = {}
        pull = self._get(pull_path, quota=quota)
        head_sha = pull["head"]["sha"]
        if not isinstance(head_sha, str) or not head_sha:
            raise RetryableError("missing GitHub head reference")
        if pull["state"] not in {"open", "closed"} or type(pull["merged"]) is not bool:
            raise RetryableError("invalid GitHub PR state")
        pr_state = "merged" if pull["merged"] else pull["state"]
        if pr_state in {"merged", "closed"}:
            # PR termination is independent of check/metadata availability.
            return Snapshot(target, pr_state, head_sha, (), time.time(),
                            f"https://github.com/{repo}/pull/{target.number}",
                            rate_limit_remaining=quota.get("remaining"))
        base_ref = pull["base"]["ref"]
        if not isinstance(base_ref, str) or not base_ref:
            raise RetryableError("missing GitHub base reference")
        required = self._required_checks(repo, base_ref, quota=quota)
        runs = self._check_runs(repo, head_sha, required, quota=quota)
        # The combined status endpoint returns the latest result per context,
        # with an explicit SHA and total_count; its contexts still paginate.
        for status in self._pages(f"/repos/{repo}/commits/{head_sha}/status", "statuses",
                                  sha=head_sha, quota=quota):
            name, state = status["context"], status["state"]
            if not isinstance(name, str) or not name or not isinstance(state, str):
                raise RetryableError("malformed GitHub commit status")
            runs.append(CheckRun(name, "completed" if state in {"success", "failure", "error"} else "queued",
                                 "failure" if state == "error" else state,
                                 head_sha, source="status", run_id=status["id"]))
        # A push or retarget while pages were being read invalidates this
        # snapshot. The next poll starts again from the new head/base.
        fresh = self._get(pull_path, quota=quota)
        if type(fresh["merged"]) is not bool:
            raise RetryableError("invalid GitHub PR state")
        if (fresh["head"]["sha"], fresh["base"]["ref"], fresh["state"], fresh["merged"]) != (
            head_sha, base_ref, pull["state"], pull["merged"]
        ):
            raise RetryableError("GitHub PR changed during snapshot fetch")
        return Snapshot(target=target, pr_state=pr_state, head_sha=head_sha,
                        checks=tuple(runs), fetched_at=time.time(),
                        url=f"https://github.com/{repo}/pull/{target.number}",
                        required_checks=required, checks_complete=True,
                        rate_limit_remaining=quota.get("remaining"))

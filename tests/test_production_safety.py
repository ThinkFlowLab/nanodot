"""Regression coverage through real factories and CLI entry points."""

import io
import json
import urllib.error

import pytest

from nanodot.cli import main
from nanodot.core.activity import ActivityLog
from nanodot.core.config import Config
from nanodot.core.redaction import Redactor
from nanodot.core.tasks import TaskStore
from nanodot.native.secrets_file import FileSecretStore

SECRET = "ghp-production-redaction-123"
API_KEY = "sk-production-redaction-456"












def test_nested_redaction_preserves_input_and_scrubs_longest_secret(home):
    secrets = FileSecretStore()
    secrets.set("api-key", "abc")
    secrets.set("github-token", "abcdef")
    original = {"abcdef": {"checks": [{"name": "abcdef"}], "tuple": ("abc", None)}}
    cleaned = Redactor(secrets).scrub_dict(original)
    assert cleaned == {"***": {"checks": [{"name": "***"}], "tuple": ("***", None)}}
    assert original["abcdef"]["checks"][0]["name"] == "abcdef"










def test_secret_stdin_input_does_not_echo(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    assert main(["config", "set", "github-token", "-"]) == 0
    assert FileSecretStore().get("github-token") == SECRET
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_secret_omitted_without_terminal_requires_explicit_stdin(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET))
    assert main(["config", "set", "github-token"]) == 1
    assert "stdin" in capsys.readouterr().err
    assert FileSecretStore().get("github-token") is None


def test_secret_hidden_prompt(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: SECRET)
    assert main(["config", "set", "github-token"]) == 0
    assert FileSecretStore().get("github-token") == SECRET
    assert SECRET not in capsys.readouterr().out




@pytest.mark.parametrize("flag", ["--notify", "--stop"])
def test_cli_rejects_unsupported_conditions_before_preview(home, capsys, flag):
    FileSecretStore().set("github-token", SECRET)
    assert main(["watch", "add", "o/r#1", flag, "only on merge", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "About to create" not in captured.out
    assert TaskStore().list() == []


def test_watch_listing_does_not_construct_fetcher_provider_notifier(home, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("read-only list must not initialize adapters")

    monkeypatch.setattr("nanodot.native.notifier.NativeNotifier", forbidden)
    monkeypatch.setattr("nanodot.native.github_client.GitHubSnapshotFetcher", forbidden)
    assert main(["watch", "list"]) == 0


def test_legacy_plaintext_secret_config_is_masked_and_removed_after_reset(home, capsys):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"github_token": SECRET, "ordinary": "ok"}))
    assert main(["config", "list"]) == 0
    output = capsys.readouterr().out
    assert SECRET not in output
    assert "github_token=***" in output
    assert main(["config", "set", "github_token", "replacement-secret"]) == 0
    assert "github_token" not in Config().keys()
    assert FileSecretStore().get("github_token") == "replacement-secret"
    (home / "config.json").write_text(json.dumps({"github_token": SECRET}))
    assert main(["config", "unset", "github_token"]) == 0
    assert "github_token" not in Config().keys()
    assert FileSecretStore().get("github_token") is None


@pytest.mark.parametrize("target", [SECRET, f"{SECRET}/repo#1"])
def test_cli_rejects_secret_in_target_without_display_or_persistence(home, capsys, target):
    FileSecretStore().set("github-token", SECRET)
    assert main(["watch", "add", target, "--yes"]) == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert TaskStore().list() == []







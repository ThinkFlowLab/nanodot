"""Secret safety regressions through the config CLI and real stores."""

import io
import json

from nanodot.cli import main
from nanodot.core.config import Config
from nanodot.core.redaction import Redactor
from nanodot.native.secrets_file import FileSecretStore

SECRET = "ghp-production-redaction-123"



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

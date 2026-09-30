"""Regression coverage through real factories and CLI entry points."""

import io
import json
import urllib.error

import pytest

from nanodot.cli import main
from nanodot.core.activity import ActivityLog
from nanodot.core.config import Config
from nanodot.core.memory import MemoryStore
from nanodot.core.redaction import Redactor
from nanodot.core.tasks import TaskStore
from nanodot.native.inference_api import APIInferenceProvider, configured_provider
from nanodot.native.secrets_file import FileSecretStore
from nanodot.ports.inference import ProviderError, StateChange

SECRET = "ghp-production-redaction-123"
API_KEY = "sk-production-redaction-456"


def response(text):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


def configured(home):
    secrets = FileSecretStore()
    secrets.set("github-token", SECRET)
    secrets.set("api-key", API_KEY)
    Config().set("model-base-url", "https://model.test/v1")
    Config().set("model-name", "test-model")
    return configured_provider()


def test_real_provider_factory_scrubs_every_outbound_value(home, monkeypatch):
    provider = configured(home)
    requests = []

    def urlopen(request, **kwargs):
        requests.append(request)
        return response(f"redact {SECRET}")

    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", urlopen)
    assert provider.summarize(StateChange(
        kind=SECRET, summary=SECRET, head_sha=SECRET,
        pr_state=SECRET, url=f"https://example.test/{SECRET}",
        checks=((f"ci-{SECRET}", SECRET),),
    )) == "redact ***"
    raw = requests[0].data.decode()
    assert SECRET not in raw
    assert API_KEY not in raw
    assert requests[0].headers["Authorization"] == f"Bearer {API_KEY}"


def test_real_cli_intent_scrubs_input_output_and_retained_task(home, monkeypatch, capsys):
    configured(home)
    bodies = []

    def urlopen(request, **kwargs):
        bodies.append(request.data.decode())
        return response(json.dumps({"target": "o/r#1", "purpose": f"watch {SECRET}"}))

    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", urlopen)
    assert main(["watch", "add", "--intent", f"watch with {SECRET}", "--yes"]) == 0
    assert len(bodies) == 1
    assert SECRET not in bodies[0]
    assert SECRET not in capsys.readouterr().out
    assert SECRET not in TaskStore().list()[0].purpose
    assert SECRET.encode() not in (home / "nanodot.db").read_bytes()


def test_real_memory_commands_redact_and_write_contentless_tombstone(home, capsys):
    FileSecretStore().set("github-token", SECRET)
    assert main(["memory", "add", f"deploy {SECRET}"]) == 0
    memory = MemoryStore()
    item = memory.list()[0]
    assert item.content == "deploy ***"
    assert main(["memory", "edit", item.id, "--content", f"new {SECRET}"]) == 0
    assert main(["memory", "propose", f"proposal {SECRET}"]) == 0
    proposed = memory.list(status="proposed")[0]
    assert main(["memory", "confirm", proposed.id]) == 0
    assert main(["memory", "list"]) == 0
    assert SECRET not in capsys.readouterr().out
    assert SECRET.encode() not in (home / "nanodot.db").read_bytes()
    assert main(["memory", "rm", item.id]) == 0
    assert memory.get(item.id) is None
    tombstones = ActivityLog().query(kinds=("memory-deleted",))
    assert len(tombstones) == 1
    assert item.id in tombstones[0].message
    assert "deploy" not in tombstones[0].message
    assert "new " not in tombstones[0].message
    assert SECRET not in tombstones[0].message


def test_nested_redaction_preserves_input_and_scrubs_longest_secret(home):
    secrets = FileSecretStore()
    secrets.set("api-key", "abc")
    secrets.set("github-token", "abcdef")
    original = {"abcdef": {"checks": [{"name": "abcdef"}], "tuple": ("abc", None)}}
    cleaned = Redactor(secrets).scrub_dict(original)
    assert cleaned == {"***": {"checks": [{"name": "***"}], "tuple": ("***", None)}}
    assert original["abcdef"]["checks"][0]["name"] == "abcdef"


def test_provider_errors_do_not_echo_known_secrets(home, monkeypatch):
    provider = configured(home)

    def urlopen(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 503, "bad", {}, io.BytesIO(SECRET.encode()))

    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", urlopen)
    with pytest.raises(ProviderError) as error:
        provider.parse_intent("watch o/r#1")
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("fence", ["json", ""])
def test_provider_accepts_fenced_json(home, monkeypatch, fence):
    provider = configured(home)
    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", lambda *a, **k: response(
        f'```{fence}\n{{"target": "o/r#1", "purpose": "watch"}}\n```'
    ))
    assert provider.parse_intent("watch").target == "o/r#1"


@pytest.mark.parametrize("content", [None, {}, [], 7])
def test_non_text_provider_content_is_typed_error(home, monkeypatch, content):
    provider = configured(home)
    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", lambda *a, **k: response(content))
    with pytest.raises(ProviderError):
        provider.summarize(StateChange(kind="test", summary="test"))


def test_direct_provider_protects_own_key_and_bounds_http_timeout(home, monkeypatch):
    calls = []
    provider = APIInferenceProvider(api_key=API_KEY)

    def urlopen(request, **kwargs):
        calls.append((request, kwargs))
        return response("summary")

    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", urlopen)
    provider.summarize(StateChange(kind="test", summary=API_KEY))
    assert API_KEY not in calls[0][0].data.decode()
    assert calls[0][1]["timeout"] <= 5


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

    monkeypatch.setattr("nanodot.native.inference_api.configured_provider", forbidden)
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


def test_invalid_provider_url_degrades_to_explicit_cli_target(home, capsys):
    configured(home)
    Config().set("model-base-url", "bad url")
    assert main(["watch", "add", "o/r#1", "--intent", "watch o/r#1", "--yes"]) == 0
    assert "intent parsing failed" in capsys.readouterr().err
    assert len(TaskStore().list()) == 1


def test_direct_provider_escaped_key_scrubbed_before_serialization(home, monkeypatch):
    key = 'key"with\\quotes\nand-newline'
    provider = APIInferenceProvider(api_key=key)
    bodies = []

    def urlopen(request, **kwargs):
        bodies.append(json.loads(request.data))
        return response("summary")

    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", urlopen)
    provider.summarize(StateChange(kind="x", summary=key, checks=((key, "failure"),)))
    payload = json.loads(bodies[0]["messages"][1]["content"])
    assert payload["summary"] == "***"
    assert payload["checks"][0]["name"] == "***"


def test_direct_provider_intent_fields_and_raw_scrub_decoded_key(home, monkeypatch):
    key = 'key"with\\quotes'
    provider = APIInferenceProvider(api_key=key)
    monkeypatch.setattr("nanodot.native.inference_api.authenticated_urlopen", lambda *a, **k: response(
        json.dumps({"target": "o/r#1", "purpose": key, "ignored": key})
    ))
    draft = provider.parse_intent("watch")
    assert draft.purpose == "***"
    assert json.loads(draft.raw) == {"target": "o/r#1", "purpose": "***"}

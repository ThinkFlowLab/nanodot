"""Secret store + redaction acceptance tests (issue #4)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from nanodot.core.config import Config
from nanodot.core.redaction import Redactor, is_secret_name
from nanodot.native.secrets_file import FileSecretStore

SECRET = "ghp_verysecretvalue123"


def test_secrets_roundtrip_and_isolation(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    assert store.get("github-token") == SECRET
    store.set("api-key", "sk-test-456")
    assert store.names() == ["api-key", "github-token"]

    # Raw secret values appear in no file under the data home except the store.
    for path in home.rglob("*"):
        if not path.is_file():
            continue
        content = path.read_text(errors="replace")
        if path.name == "secrets.json":
            continue
        assert SECRET not in content, path
        assert "sk-test-456" not in content, path


def test_secret_file_permissions(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    mode = stat.S_IMODE(os.stat(home / "secrets.json").st_mode)
    assert mode == 0o600


def test_unset_removes_secret(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    store.unset("github-token")
    assert store.get("github-token") is None
    assert "secrets.json" in [p.name for p in home.iterdir()]


def test_redactor_scrubs_embedded_values(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    redactor = Redactor(store)
    dirty = f"watch owner/repo#1 using token {SECRET} please"
    clean = redactor.scrub(dirty)
    assert SECRET not in clean
    assert "***" in clean
    assert redactor.contains_secret(dirty)
    assert not redactor.contains_secret(clean)


def test_redactor_scrubs_dict_values(home: Path) -> None:
    store = FileSecretStore()
    store.set("api-key", "sk-test-456")
    redactor = Redactor(store)
    scrubbed = redactor.scrub_dict({"title": "sk-test-456 in title", "count": 3})
    assert scrubbed == {"title": "*** in title", "count": 3}


def test_config_routes_secret_names_to_store(home: Path) -> None:
    from nanodot.cli import main

    assert is_secret_name("github-token")
    assert is_secret_name("api-key")
    assert not is_secret_name("poll-cadence")

    assert main(["config", "set", "github-token", SECRET]) == 0
    assert main(["config", "set", "poll-cadence", "300"]) == 0

    config = Config()
    store = FileSecretStore()
    assert store.get("github-token") == SECRET
    assert config.get("poll-cadence") == "300"
    assert "github-token" not in (home / "config.json").read_text()


def test_config_list_masks_secrets(home: Path, capsys) -> None:
    from nanodot.cli import main

    main(["config", "set", "github-token", SECRET])
    main(["config", "set", "poll-cadence", "300"])
    capsys.readouterr()
    assert main(["config", "list"]) == 0
    out = capsys.readouterr().out
    assert "poll-cadence=300" in out
    assert f"github-token={SECRET}" not in out
    assert "github-token=***" in out

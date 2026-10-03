"""Secret store + redaction acceptance tests (issue #4)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("name", [
    "github_token", "ACCESS_TOKEN", "api_key", "password", "pat", "token",
    "secret", "key", "deploy-password", "client_secret", "db_passwd",
])
def test_common_secret_names_route_outside_plain_config(home: Path, name: str) -> None:
    from nanodot.cli import main

    assert is_secret_name(name)
    assert main(["config", "set", name, SECRET]) == 0
    assert FileSecretStore().get(name) == SECRET
    assert Config().get(name) is None
    assert not (home / "config.json").exists()


@pytest.mark.parametrize("name", ["poll-cadence", "model-name", "keybinding", "pattern"])
def test_non_secret_names_remain_plain_config(name: str) -> None:
    assert not is_secret_name(name)


def test_atomic_save_replaces_inode_and_restores_private_mode(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    path = home / "secrets.json"
    old_inode = path.stat().st_ino
    path.chmod(0o644)

    store.set("github-token", "rotated-token")

    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.get("github-token") == "rotated-token"
    contents = sorted(p.name for p in home.iterdir())
    assert contents == ["secrets.json", "secrets.json.lock"]


@pytest.mark.parametrize("operation", ["get", "set", "unset", "names"])
def test_secret_store_rejects_existing_symlink(home: Path, operation: str) -> None:
    store = FileSecretStore()
    victim = home.parent / "outside.json"
    original = '{"github-token": "outside-value"}'
    victim.write_text(original)
    (home / "secrets.json").symlink_to(victim)

    args = {"get": ("github-token",), "set": ("github-token", SECRET),
            "unset": ("github-token",), "names": ()}[operation]
    with pytest.raises(ValueError, match="regular file"):
        getattr(store, operation)(*args)
    assert victim.read_text() == original
    assert (home / "secrets.json").is_symlink()


def test_secret_store_rejects_dangling_symlink(home: Path) -> None:
    store = FileSecretStore()
    victim = home.parent / "must-not-create.json"
    (home / "secrets.json").symlink_to(victim)
    with pytest.raises(ValueError, match="regular file"):
        store.set("github-token", SECRET)
    assert not victim.exists()


def test_atomic_replace_does_not_follow_late_symlink(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    victim = home.parent / "untouched.json"
    victim.write_text("untouched")
    original_replace = os.replace

    def plant_link_then_replace(source, destination):
        Path(destination).unlink()
        Path(destination).symlink_to(victim)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", plant_link_then_replace)
    store.set("api-key", "second-secret")
    assert victim.read_text() == "untouched"
    assert not (home / "secrets.json").is_symlink()
    assert store.get("api-key") == "second-secret"


def test_rotation_between_lstat_and_open_retries_instead_of_failing(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSecretStore()
    store.set("github-token", "old-token")
    path = home / "secrets.json"
    real_open = os.open
    rotations = 0

    def rotating_open(file, flags, *args, **kwargs):
        # Land one concurrent atomic rotation inside the lstat→open window:
        # replace the directory entry right before the real open happens.
        nonlocal rotations
        if Path(file) == path:
            rotations += 1
            if rotations == 1:
                FileSecretStore()._save({"github-token": "new-token"})
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", rotating_open)
    assert store.get("github-token") == "new-token"
    assert rotations == 1  # the raced entry resolves to the opened rotation


def test_continuous_rotation_never_breaks_readers(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", "old-token")
    writer = FileSecretStore()
    for rotation in range(200):
        writer.set("github-token", f"rotated-{rotation}")
        # Every read must observe some complete value, never raise.
        assert store.get("github-token").startswith(("old", "rotated-"))


@pytest.mark.parametrize("nofollow", [True, False])
def test_loading_rejects_symlink_swapped_after_inspection(
    home: Path, monkeypatch: pytest.MonkeyPatch, nofollow: bool,
) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    path = home / "secrets.json"
    victim = home.parent / "outside.json"
    victim.write_text('{"github-token":"must-not-read"}')
    original_open = os.open
    if not nofollow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

    def swap_then_open(filename, flags, *args, **kwargs):
        if Path(filename) == path:
            path.unlink()
            path.symlink_to(victim)
        return original_open(filename, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(OSError):
        store.get("github-token")


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_failed_atomic_save_preserves_original_and_removes_temporary(
    home: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import nanodot.native.secrets_file as module

    store = FileSecretStore()
    store.set("github-token", SECRET)
    original = (home / "secrets.json").read_bytes()

    def fail(*args, **kwargs):
        if failure == "write":
            args[1].write('{"partial":')
        raise OSError("injected interrupted save")

    if failure == "write":
        monkeypatch.setattr(module.json, "dump", fail)
    else:
        monkeypatch.setattr(module.os, failure, fail)
    with pytest.raises(OSError, match="interrupted save"):
        store.set("api-key", "second-secret")

    assert (home / "secrets.json").read_bytes() == original
    contents = sorted(p.name for p in home.iterdir())
    assert contents == ["secrets.json", "secrets.json.lock"]


def test_atomic_save_syncs_file_before_replace_then_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSecretStore()
    events = []
    original_replace = os.replace

    def record_sync(fd):
        mode = os.fstat(fd).st_mode
        events.append("directory-sync" if stat.S_ISDIR(mode) else "file-sync")
        if stat.S_ISREG(mode):
            assert stat.S_IMODE(mode) == 0o600

    def record_replace(source, destination):
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(os, "fsync", record_sync)
    monkeypatch.setattr(os, "replace", record_replace)
    store.set("github-token", SECRET)
    assert events == (["file-sync", "replace", "directory-sync"]
                      if os.name == "posix" else ["file-sync", "replace"])


def test_existing_redactor_observes_secret_rotation(home: Path) -> None:
    store = FileSecretStore()
    store.set("github-token", SECRET)
    redactor = Redactor(store)
    assert redactor.scrub(SECRET) == "***"
    store.set("github-token", "newly-rotated-secret")
    assert redactor.scrub("newly-rotated-secret") == "***"

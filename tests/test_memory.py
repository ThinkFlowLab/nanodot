"""Memory subsystem acceptance tests (issue #12)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fakes import FAILURE, SUCCESS, FakeClock, FakeGitHub, FakeProvider, FakeSink

from nanodot.core.activity import ActivityLog
from nanodot.core.memory import (
    STATUS_CONFIRMED,
    STATUS_PROPOSED,
    MemoryStore,
)
from nanodot.core.runner import TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.native.secrets_file import FileSecretStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#12")
SRC = Path(__file__).resolve().parent.parent / "src" / "nanodot"


@pytest.fixture()
def memory(home: Path) -> MemoryStore:
    return MemoryStore(activity=ActivityLog())


# -- write-path enforcement ---------------------------------------------------


def test_user_statements_enter_confirmed(memory: MemoryStore) -> None:
    item = memory.add_user("only notify failures for thinkflowlab repos")
    assert item.status == STATUS_CONFIRMED
    assert item.kind == "preference"
    assert item.provenance == {"source": "user"}


def test_proposals_stay_proposed_until_confirmed(memory: MemoryStore) -> None:
    item = memory.propose("prefers short summaries", source="model")
    assert item.status == STATUS_PROPOSED
    assert item not in [
        m for m in memory.list(status=STATUS_CONFIRMED)
    ]
    confirmed = memory.confirm(item.id)
    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.expires_at is None
    with pytest.raises(ValueError):
        memory.confirm(item.id)  # already confirmed


def test_no_code_path_lets_model_output_become_confirmed() -> None:
    """AST boundary: the only callers of the confirmed-write paths are the
    user surface (cli) and the evidenced-outcome recorder (runner). The
    provider/adapter modules never write memory at all."""
    allowed_callers = {
        "add_user": {"cli.py"},
        "confirm": {"cli.py"},
        "add_observation": {"runner.py"},
        "propose": {"cli.py"},
    }
    for py in SRC.rglob("*.py"):
        if py.parent.name == "core" and py.name == "memory.py":
            continue
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in allowed_callers:
                    assert py.name in allowed_callers[node.func.attr], (
                        f"{py.name} calls memory.{node.func.attr}"
                    )


def test_loop_with_proposing_provider_never_confirms(home: Path) -> None:
    """End-to-end: a provider that 'proposes' memory leaves only proposals."""
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    memory = MemoryStore(path=home / "nanodot.db", activity=activity)
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")
    provider = FakeProvider()
    provider.summaries = [
        "You seem to prefer immediate failure reports — remember this?"
    ]
    sink = FakeSink()
    task = store.create(Task(target=TARGET, purpose="p", next_check_at=0.0))
    loop = TaskLoop(store, github, sink, activity, provider=provider, memory=memory)

    loop.run_once(store.get(task.id), FakeClock().now)

    # The summary text went to a notification, not into memory as fact.
    assert memory.list() == []
    assert sink.events[0].summary == provider.summaries[0]


# -- evidenced observations -----------------------------------------------------


def test_terminal_outcome_auto_records_observation_with_provenance(
    home: Path,
) -> None:
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    memory = MemoryStore(path=home / "nanodot.db", activity=activity)
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", SUCCESS, sha="s1")
    sink = FakeSink()
    task = store.create(Task(target=TARGET, purpose="p", next_check_at=0.0))
    loop = TaskLoop(store, github, sink, activity, memory=memory)

    loop.run_once(store.get(task.id), FakeClock().now)

    items = memory.list()
    assert len(items) == 1
    assert items[0].kind == "observation"
    assert items[0].status == STATUS_CONFIRMED
    assert items[0].provenance["source"] == f"task:{task.id}"
    assert items[0].provenance["evidence"].endswith("@s1")
    assert str(TARGET) in items[0].content


# -- deletion and tombstones ------------------------------------------------------


def test_deletion_removes_content_and_tombstones_activity(
    home: Path, memory: MemoryStore
) -> None:
    item = memory.add_user("my deploy window is Friday morning")
    memory.remove(item.id)

    assert memory.get(item.id) is None
    assert memory.context_for_prompts() == []
    # The raw DB file no longer contains the content.
    raw = (home / "nanodot.db").read_bytes()
    assert b"deploy window" not in raw
    # Activity shows the fact of deletion, not the content.
    log = ActivityLog()
    tombstones = log.query(task_id="memory", kinds=("memory-deleted",))
    assert len(tombstones) == 1
    assert "deploy window" not in tombstones[0].message
    assert item.id in tombstones[0].message


# -- provenance on every item -------------------------------------------------------


def test_every_item_carries_provenance(memory: MemoryStore) -> None:
    a = memory.add_user("prefers terse output")
    b = memory.propose("likes morning reports", source="model")
    for item in (a, b):
        assert item.provenance.get("source")


# -- proposal expiry -----------------------------------------------------------------


def test_proposals_expire_after_window(memory: MemoryStore) -> None:
    now = 1_000_000.0
    item = memory.propose("might prefer x", at=now, expires_in=14 * 24 * 3600)
    assert memory.sweep_expired(now=now + 100) == 0  # still within window
    assert memory.sweep_expired(now=now + 14 * 24 * 3600 + 1) == 1
    assert memory.get(item.id) is None


@pytest.mark.parametrize("elapsed", [60, 61])
def test_expired_proposal_cannot_be_confirmed_without_sweep(home: Path, elapsed: int) -> None:
    clock = FakeClock()
    memory = MemoryStore(clock=clock)
    item = memory.propose("pending preference", expires_in=60)
    clock.advance(elapsed)
    with pytest.raises(ValueError, match="expired"):
        memory.confirm(item.id)
    assert memory.get(item.id) is None
    assert memory.context_for_prompts() == []


@pytest.mark.parametrize("read", ["get", "list"])
def test_memory_reads_expire_proposals_without_sweep(home: Path, read: str) -> None:
    clock = FakeClock()
    activity = ActivityLog()
    memory = MemoryStore(clock=clock, activity=activity)
    expired = memory.propose("old proposal", expires_in=60)
    active = memory.propose("fresh proposal", expires_in=120)
    confirmed = memory.add_user("confirmed preference")
    clock.advance(60)
    if read == "get":
        assert memory.get(expired.id) is None
    else:
        assert {item.id for item in memory.list()} == {active.id, confirmed.id}
    assert memory.get(active.id) == active
    assert memory.get(confirmed.id) == confirmed
    tombstones = activity.query(task_id="memory", kinds=("memory-deleted",))
    assert len(tombstones) == 1
    assert expired.id in tombstones[0].message
    assert "old proposal" not in tombstones[0].message


def test_memory_startup_removes_expired_proposals(home: Path) -> None:
    clock = FakeClock()
    memory = MemoryStore(clock=clock)
    expired = memory.propose("expired private preference", expires_in=60)
    confirmed = memory.add_user("confirmed preference")
    memory.close()
    clock.advance(60)
    memory = MemoryStore(clock=clock)
    assert memory.get(expired.id) is None
    assert memory.list() == [confirmed]
    assert b"expired private preference" not in (home / "nanodot.db").read_bytes()


def test_confirmed_proposal_survives_its_original_expiry(home: Path) -> None:
    clock = FakeClock()
    memory = MemoryStore(clock=clock)
    proposal = memory.propose("keep this preference", expires_in=60)
    clock.advance(59)
    confirmed = memory.confirm(proposal.id)
    clock.advance(2)
    assert memory.get(proposal.id) == confirmed


def test_confirmation_uses_the_supplied_time(home: Path) -> None:
    clock = FakeClock()
    memory = MemoryStore(clock=clock)
    proposal = memory.propose("pending preference", expires_in=60)
    with pytest.raises(ValueError, match="expired"):
        memory.confirm(proposal.id, at=clock.time() + 60)


# -- the loop runs with an empty memory store -------------------------------------------


def test_watch_loop_works_with_empty_memory(home: Path) -> None:
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    memory = MemoryStore(path=home / "nanodot.db", activity=activity)  # empty
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")
    sink = FakeSink()
    task = store.create(Task(target=TARGET, purpose="p", next_check_at=0.0))
    loop = TaskLoop(store, github, sink, activity, memory=memory)

    assert memory.list() == []
    from nanodot.core.runner import RunOutcome

    assert loop.run_once(store.get(task.id), FakeClock().now) is RunOutcome.OK
    assert "checks-failed" in sink.kinds()[0]


# -- secrets never persist --------------------------------------------------------------


def test_secrets_never_persist_in_memory(home: Path) -> None:
    from nanodot.core.redaction import Redactor

    secret = "ghp_memoryleak888"
    FileSecretStore().set("github-token", secret)
    memory = MemoryStore(
        redactor=Redactor(FileSecretStore()), activity=ActivityLog()
    )
    memory.add_user(f"use token {secret} for deploys")
    memory.propose(f"token looks like {secret}", source="model")
    raw = (home / "nanodot.db").read_bytes()
    assert secret.encode() not in raw


# -- CLI surface ------------------------------------------------------------------------


def test_cli_memory_roundtrip(home: Path, capsys) -> None:
    from nanodot.cli import main

    assert main(["memory", "add", "only failures for this repo"]) == 0
    assert main(["memory", "propose", "prefers short summaries"]) == 0
    assert main(["memory", "list"]) == 0
    out = capsys.readouterr().out
    assert "only failures" in out
    assert "[proposed ]" in out and "[confirmed]" in out

    item_id = [
        line.split()[0] for line in out.splitlines() if "short summaries" in line
    ][0]
    assert main(["memory", "confirm", item_id]) == 0
    assert main(["memory", "edit", item_id, "--content", "prefers one-line summaries"]) == 0
    assert main(["memory", "show", item_id]) == 0
    assert "one-line summaries" in capsys.readouterr().out
    assert main(["memory", "rm", item_id]) == 0
    assert main(["memory", "show", item_id]) == 1


def test_relevant_to_matches_recorded_pr_identity(home: Path) -> None:
    store = MemoryStore()
    # Runner-recorded observations embed the target as 'owner/repo#12: ...'.
    observation = store.add_observation(
        "thinkflowlab/nanodot#12: required checks passed",
        task_id="task-1",
        evidence_ref="activity:1",
    )

    hits = store.relevant_to("thinkflowlab/nanodot#12 ping me when done")
    assert [item.id for item in hits] == [observation.id]

    unrelated = store.add_user("prefer short summaries")
    assert unrelated.id not in [item.id for item in store.relevant_to(
        "thinkflowlab/nanodot#12 ping me when done")]


def test_empty_memory_content_is_rejected(home: Path) -> None:
    store = MemoryStore()
    for content in ("", "   "):
        with pytest.raises(ValueError, match="must not be empty"):
            store.add_user(content)
        with pytest.raises(ValueError, match="must not be empty"):
            store.propose(content)
    item = store.add_user("real content")
    with pytest.raises(ValueError, match="must not be empty"):
        store.edit(item.id, "")
    assert store.count() == 1


def test_memory_list_reports_hidden_older_items(home: Path, capsys) -> None:
    from nanodot.cli import main

    for index in range(202):
        assert main(["memory", "add", f"preference number {index:03d}"]) == 0
    capsys.readouterr()
    assert main(["memory", "list"]) == 0
    out = capsys.readouterr().out
    assert "preference number 201" in out
    assert "... and 2 older item(s) not shown" in out

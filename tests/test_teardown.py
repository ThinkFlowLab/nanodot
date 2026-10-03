"""Registrations-are-effects shutdown: reverse unwind, residue-free stop
(docs/design/adapter-seam.md, admission discipline 2 / issue #37)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fakes import FAILURE, FakeGitHub, FakeSink

from nanodot.cli import main
from nanodot.core.activity import ActivityLog
from nanodot.core.memory import MemoryStore
from nanodot.core.runner import RunOutcome, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.core.teardown import Teardown
from nanodot.ports.inference import TaskDraft

TARGET = PRTarget.parse("thinkflowlab/nanodot#12")


# -- Teardown: the registry --------------------------------------------------


def test_teardown_unwinds_in_reverse_order_and_runs_once() -> None:
    order: list[str] = []
    teardown = Teardown()
    teardown.register("first", lambda: order.append("first"))
    teardown.register("second", lambda: order.append("second"))
    teardown.register("third", lambda: order.append("third"))

    assert teardown.run() == []
    assert order == ["third", "second", "first"]
    assert teardown.run() == []  # a second stop unwinds nothing
    assert order == ["third", "second", "first"]


def test_teardown_runs_every_step_and_names_only_the_failures() -> None:
    order: list[str] = []
    teardown = Teardown()

    def failing() -> None:
        order.append("second")
        raise RuntimeError("private detail that must not be reported verbatim")

    teardown.register("first", lambda: order.append("first"))
    teardown.register("second", failing)
    teardown.register("third", lambda: order.append("third"))

    assert teardown.run() == ["second"]
    assert order == ["third", "second", "first"]  # every step ran, in reverse


def test_teardown_survives_base_exception_in_a_disposer() -> None:
    """A second Ctrl-C landing mid-unwind costs one step, not the rest."""
    order: list[str] = []
    teardown = Teardown()

    def interrupted() -> None:
        order.append("second")
        raise KeyboardInterrupt

    teardown.register("first", lambda: order.append("first"))
    teardown.register("second", interrupted)
    teardown.register("third", lambda: order.append("third"))

    assert teardown.run() == ["second"]
    assert order == ["third", "second", "first"]  # 'first' still ran


# -- TaskLoop.close: drain the summary, drop the locks -----------------------


class _GatedProvider:
    """Summarize blocks until released — a misbehaving, slow provider."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def summarize(self, change) -> str:
        self.entered.set()
        self.release.wait(5.0)
        time.sleep(0.1)  # linger after release so a drain is observable
        return "model summary"

    def parse_intent(self, text: str) -> TaskDraft:
        return TaskDraft()


def _loop(home: Path, budget: float) -> tuple[TaskStore, TaskLoop, Task, _GatedProvider]:
    store = TaskStore(path=home / "nanodot.db")
    activity = ActivityLog(path=home / "nanodot.db")
    github = FakeGitHub(TARGET)
    github.set_pr("open", head_sha="s1")
    github.add_check("ci", FAILURE, sha="s1")  # notable → a summary is requested
    provider = _GatedProvider()
    loop = TaskLoop(
        store, github, FakeSink(), activity,
        provider=provider, summary_budget_seconds=budget,
    )
    task = store.create(
        Task(target=TARGET, purpose="watch", cadence_seconds=300, next_check_at=0.0)
    )
    return store, loop, task, provider


def test_task_loop_close_is_bounded_even_with_a_hung_provider(home: Path) -> None:
    store, loop, task, provider = _loop(home, budget=0.2)
    outcomes: list[RunOutcome] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(loop.run_once(store.get(task.id), 0.0))
    )
    worker.start()
    assert provider.entered.wait(2.0), "summary never reached the provider"

    started = time.monotonic()
    loop.close()
    assert time.monotonic() - started < 2.0, "close hung on the summary drain"

    provider.release.set()
    worker.join(5.0)
    assert outcomes == [RunOutcome.OK]  # the run itself was never interrupted

    loop.close()  # idempotent with nothing in flight


def test_task_loop_close_drains_a_finishing_summary(home: Path) -> None:
    store, loop, task, provider = _loop(home, budget=5.0)
    outcomes: list[RunOutcome] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(loop.run_once(store.get(task.id), 0.0))
    )
    worker.start()
    assert provider.entered.wait(2.0)
    provider.release.set()

    started = time.monotonic()
    loop.close()
    assert time.monotonic() - started >= 0.08, "close did not wait for the provider"
    worker.join(5.0)
    assert outcomes == [RunOutcome.OK]


# -- The real wiring: a full pass unwinds under the lease --------------------


def test_runner_once_unwinds_every_registration_in_reverse(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(TaskStore, "close", lambda self: order.append("task-store"))
    monkeypatch.setattr(ActivityLog, "close", lambda self: order.append("activity-log"))
    monkeypatch.setattr(MemoryStore, "close", lambda self: order.append("memory-store"))
    from nanodot.native.notifier import NativeNotifier

    monkeypatch.setattr(NativeNotifier, "close", lambda self: order.append("inbox-sink"))
    monkeypatch.setattr(TaskLoop, "close", lambda self: order.append("task-loop"))

    assert main(["runner", "--once"]) == 0
    assert order == ["task-loop", "inbox-sink", "memory-store", "activity-log", "task-store"]


def test_wiring_failure_still_unwinds_registered_resources(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wiring step that fails after stores opened must not leak them."""
    order: list[str] = []
    monkeypatch.setattr(TaskStore, "close", lambda self: order.append("task-store"))
    monkeypatch.setattr(ActivityLog, "close", lambda self: order.append("activity-log"))
    monkeypatch.setattr(MemoryStore, "close", lambda self: order.append("memory-store"))
    from nanodot.native.notifier import NativeNotifier

    monkeypatch.setattr(NativeNotifier, "close", lambda self: order.append("inbox-sink"))

    def broken_provider():
        raise ValueError("secrets file unusable")

    # _wiring imports configured_provider locally at call time; patch the
    # source module so the failure lands mid-wiring, after the stores open.
    monkeypatch.setattr(
        "nanodot.native.inference_api.configured_provider", broken_provider
    )

    assert main(["runner", "--once"]) == 1
    assert "error:" in capsys.readouterr().err
    assert order == ["inbox-sink", "memory-store", "activity-log", "task-store"]


def test_runner_once_disposes_cleanly_and_leaves_readable_data(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unpatched: every real disposer ran cleanly, and the next owner of
    the data home reads the same files without trouble."""
    assert main(["runner", "--once"]) == 0
    assert "did not shut down cleanly" not in capsys.readouterr().err

    store = TaskStore(path=home / "nanodot.db")
    assert store.list() == []
    assert ActivityLog(path=home / "nanodot.db").query() == []

"""Notification sink + inbox acceptance tests (issue #9)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeGitHub

from nanodot.core.redaction import Redactor
from nanodot.core.statemachine import CHECKS_FAILED, CHECKS_PASSED, WatchEvent
from nanodot.core.tasks import PRTarget
from nanodot.native.notifier import NativeNotifier
from nanodot.native.secrets_file import FileSecretStore

TARGET = PRTarget.parse("thinkflowlab/nanodot#11")


def make_event(kind: str = CHECKS_FAILED, notable: bool = True, **overrides) -> WatchEvent:
    defaults = dict(
        kind=kind,
        message="checks failing on abc123",
        evidence={"url": "https://github.com/x/y/pull/11", "head_sha": "abc123"},
        notable=notable,
        task_id="task-1",
        at=1000.0,
    )
    defaults.update(overrides)
    return WatchEvent(**defaults)


class RecordingOsascript:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, args, **kwargs) -> None:
        self.calls.append(args)


@pytest.fixture()
def notifier(home: Path) -> NativeNotifier:
    recorder = RecordingOsascript()
    sink = NativeNotifier(
        path=home / "nanodot.db", osascript_runner=recorder
    )
    sink.recorder = recorder  # type: ignore[attr-defined]
    return sink


def test_same_event_twice_yields_one_inbox_entry_and_one_notification(
    notifier: NativeNotifier,
) -> None:
    event = make_event()
    notifier.notify(event)
    notifier.notify(event)  # replay (e.g. restart re-delivery)
    assert len(notifier.list()) == 1
    assert len(notifier.os_notifications) == 1
    assert len(notifier.recorder.calls) == 1


def test_dedup_survives_restart(home: Path, notifier: NativeNotifier) -> None:
    notifier.notify(make_event())
    notifier.close()

    reopened = NativeNotifier(
        path=home / "nanodot.db", osascript_runner=RecordingOsascript()
    )
    reopened.notify(make_event())  # same content after restart
    assert len(reopened.list()) == 1
    assert reopened.os_notifications == []
    reopened.close()


def test_distinct_events_both_delivered(notifier: NativeNotifier) -> None:
    notifier.notify(make_event(kind=CHECKS_FAILED))
    notifier.notify(make_event(kind=CHECKS_PASSED, message="all checks passed"))
    assert len(notifier.list()) == 2
    assert len(notifier.os_notifications) == 2


def test_non_notable_intermediate_polls_do_not_notify(notifier: NativeNotifier) -> None:
    notifier.notify(make_event(kind="checks-pending", notable=False))
    assert notifier.list() == []
    assert notifier.os_notifications == []


def test_inbox_entries_carry_evidence_links(notifier: NativeNotifier) -> None:
    notifier.notify(make_event())
    entry = notifier.list()[0]
    assert entry.evidence["url"].endswith("/pull/11")
    assert entry.evidence["head_sha"] == "abc123"
    assert CHECKS_FAILED in entry.message or "failing" in entry.message


def test_notification_text_contains_no_secrets(home: Path) -> None:
    secret = "ghp_notifyleak555"
    FileSecretStore().set("github-token", secret)
    sink = NativeNotifier(
        path=home / "nanodot.db",
        redactor=Redactor(FileSecretStore()),
        os_notify=False,
    )
    sink.notify(make_event(message=f"failure with token {secret}"))
    entry = sink.list()[0]
    assert secret not in entry.message
    assert secret not in sink.os_notifications[0]
    assert secret.encode() not in (home / "nanodot.db").read_bytes()


def test_os_notification_failure_is_tolerated(home: Path) -> None:
    def exploding(args, **kwargs) -> None:
        raise RuntimeError("no osascript here")

    sink = NativeNotifier(
        path=home / "nanodot.db", osascript_runner=exploding
    )
    sink.notify(make_event())
    assert len(sink.list()) == 1  # inbox still written

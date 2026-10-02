"""Notification sink port (see docs/design/adapter-seam.md, port 3)."""

from __future__ import annotations

from typing import Protocol

from nanodot.core.statemachine import WatchEvent


class NotificationSink(Protocol):
    """Delivers notable/terminal events. Must be idempotent per event —
    core identifies transitions and durable occurrence IDs; sinks deduplicate
    delivery of the same occurrence across retries and restarts."""

    def notify(self, event: WatchEvent) -> None: ...

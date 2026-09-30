"""Notification sink port (see docs/design/adapter-seam.md, port 3)."""

from __future__ import annotations

from typing import Protocol

from nanodot.core.statemachine import WatchEvent


class NotificationSink(Protocol):
    """Delivers notable/terminal events. Must be idempotent per event —
    dedup policy lives in core, implementations just deliver."""

    def notify(self, event: WatchEvent) -> None: ...

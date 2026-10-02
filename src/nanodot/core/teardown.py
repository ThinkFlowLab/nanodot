"""Registrations are effects — the shutdown discipline.

Resources register a named disposer in creation order; shutdown unwinds
in reverse. A raising disposer never blocks the unwind: every step runs,
and failures are reported by name only — never exception text, which can
carry private data. See docs/design/adapter-seam.md, admission
discipline 2.
"""

from __future__ import annotations

from collections.abc import Callable


class Teardown:
    """Collect disposers as resources are built; unwind them on stop."""

    def __init__(self) -> None:
        self._disposers: list[tuple[str, Callable[[], None]]] = []

    def register(self, name: str, disposer: Callable[[], None]) -> None:
        self._disposers.append((name, disposer))

    def run(self) -> list[str]:
        """Unwind in reverse registration order, exactly once.

        Returns the names of disposers that raised; every disposer runs
        regardless. A second call unwinds nothing.
        """
        failed: list[str] = []
        while self._disposers:
            name, disposer = self._disposers.pop()
            try:
                disposer()
            except Exception:
                failed.append(name)
        return failed

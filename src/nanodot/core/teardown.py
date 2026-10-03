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
    """Collect disposers as resources are built; unwind them on stop.

    Not thread-safe: register from the orchestrating thread that will
    later call run — the same thread in the native runner, and the
    contract an adapter host must keep.
    """

    def __init__(self) -> None:
        self._disposers: list[tuple[str, Callable[[], None]]] = []

    def register(self, name: str, disposer: Callable[[], None]) -> None:
        self._disposers.append((name, disposer))

    def run(self) -> list[str]:
        """Unwind in reverse registration order, exactly once.

        Returns the names of disposers that raised; every disposer runs
        regardless — a KeyboardInterrupt landing mid-unwind costs the step
        it interrupted, never the steps after it. A second call unwinds
        nothing.
        """
        failed: list[str] = []
        while self._disposers:
            name, disposer = self._disposers.pop()
            try:
                disposer()
            except BaseException:
                # BaseException: the unwind is the last line of defense for
                # "every step runs" — a second Ctrl-C must not leave the
                # remaining stores half-closed. Names only, never exception
                # text, which can carry private data.
                failed.append(name)
        return failed

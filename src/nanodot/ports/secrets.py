"""Secret store port (see docs/design/adapter-seam.md, port 5)."""

from __future__ import annotations

from typing import Protocol


class SecretStore(Protocol):
    """Holds secret values (GitHub PAT, inference API key).

    Values are served only through this port and must never be persisted
    anywhere else; the redaction layer (core.redaction) enforces that at
    every persistence boundary.
    """

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def unset(self, name: str) -> None: ...

    def names(self) -> list[str]: ...

"""Redaction of secret values at persistence boundaries.

Every store (task, activity, memory, inbox) and every prompt builder
scrubs text through a Redactor before persisting or sending, so secret
material can never leak into retained data or logs.
"""

from __future__ import annotations

import re

from nanodot.ports.secrets import SecretStore

MASK = "***"


class Redactor:
    """Scrubs known secret values out of arbitrary text."""

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    def _values(self) -> list[str]:
        values = [
            self._secrets.get(name) for name in self._secrets.names()
        ]
        return [v for v in values if v]

    def scrub(self, text: str) -> str:
        for value in self._values():
            if value:
                text = text.replace(value, MASK)
        return text

    def scrub_dict(self, mapping: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in mapping.items():
            if isinstance(value, str):
                out[key] = self.scrub(value)
            else:
                out[key] = value
        return out

    def contains_secret(self, text: str) -> bool:
        return any(value and value in text for value in self._values())


_SECRET_NAME_RE = re.compile(r"-token$|-key$|^token$|^secret$", re.IGNORECASE)


def is_secret_name(name: str) -> bool:
    """Config keys that name secrets and must route to the secret store."""
    return bool(_SECRET_NAME_RE.search(name))

"""Ports — the interfaces core depends on.

Each port is a small protocol; the native runner supplies implementations
(see docs/design/adapter-seam.md). Core imports nothing from
``nanodot.native``; the import boundary is enforced by test.
"""

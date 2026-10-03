"""TEMPORARY: gated write-port live acceptance (#59).

This test intentionally fails so the watcher observes failing required
checks and proposes its comment. The branch is closed and deleted after
the acceptance run.
"""

def test_intentional_failure_live_acceptance() -> None:
    assert False, "intentional: gated write-port live acceptance (#59)"

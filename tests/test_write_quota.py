"""Write quota acceptance tests (issue #53, #48 decisions 4 and 8)."""

from __future__ import annotations

from pathlib import Path

from fakes import FakeClock

from nanodot.core.write_quota import DEFAULT_WRITE_BUDGETS, WriteQuota


def test_defaults_follow_decision_8() -> None:
    assert DEFAULT_WRITE_BUDGETS == {"comment": 3, "label": 3, "approve": 1}


def test_charging_is_bounded_idempotent_and_daily(home: Path) -> None:
    quota = WriteQuota(path=home / "nanodot.db", clock=FakeClock())
    assert quota.remaining("comment", "t1", quota._clock.time()) == 3

    for i in range(3):
        assert quota.try_charge("comment", "t1", f"d{i}", quota._clock.time())
    assert quota.remaining("comment", "t1", quota._clock.time()) == 0
    assert not quota.try_charge("comment", "t1", "d3", quota._clock.time())

    # Idempotent: the same evidence digest never pays twice, even after
    # exhaustion — a re-charge of sent content stays True.
    assert quota.try_charge("comment", "t1", "d0", quota._clock.time())

    # The budget is per watch and per action.
    assert quota.remaining("comment", "t2", quota._clock.time()) == 3
    assert quota.remaining("approve", "t1", quota._clock.time()) == 1


def test_the_day_boundary_resets_with_the_clock(home: Path) -> None:
    clock = FakeClock()
    quota = WriteQuota(path=home / "nanodot.db", clock=clock)
    for i in range(3):
        assert quota.try_charge("comment", "t1", f"d{i}", clock.time())
    assert quota.remaining("comment", "t1", clock.time()) == 0

    clock.advance(24 * 3600)  # the next UTC day
    assert quota.remaining("comment", "t1", clock.time()) == 3
    assert quota.try_charge("comment", "t1", "d0", clock.time())  # new day, new charge


def test_per_watch_override_and_zero_disables(home: Path) -> None:
    quota = WriteQuota(path=home / "nanodot.db", clock=FakeClock())
    quota.set_budget("t1", "comment", 5)
    assert quota.remaining("comment", "t1", quota._clock.time()) == 5
    assert quota.remaining("comment", "t2", quota._clock.time()) == 3  # untouched

    quota.set_budget("t2", "comment", 0)
    assert not quota.try_charge(
        "comment", "t2", "d0", quota._clock.time()
    )  # zero = the action is off for the day, fail closed

    try:
        quota.set_budget("t3", "comment", -1)
        raise AssertionError("negative budgets must be rejected")
    except ValueError:
        pass


def test_unknown_actions_have_no_budget(home: Path) -> None:
    quota = WriteQuota(path=home / "nanodot.db", clock=FakeClock())
    assert quota.remaining("merge", "t1", quota._clock.time()) == 0
    assert not quota.try_charge("merge", "t1", "d0", quota._clock.time())

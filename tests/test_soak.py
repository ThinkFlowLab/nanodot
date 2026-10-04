"""Soak test — long-run daemon health (issue #101).

A compressed daemon lifetime: 3,000 scheduler ticks (~10 simulated days)
against real SQLite, one watch with the full stage-2 kit, flips and new
commits along the way. Guards the slow failure modes: steady-state memory
growth, per-tick latency drift, retention efficacy, DB growth. The
budgets are generous tripwires, not benchmarks; widening one is a
decision that requires a finding, not a fix.
"""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

from fakes import COMPLETED, FAILURE, FakeClock, FakeGitHub, FakeSink
from fakes import SUCCESS
from nanodot.core.activity import ActivityLog
from nanodot.core.runner import OBSERVATION_RETENTION
from nanodot.core.runner import DIGEST, FLAKY, STALE, TaskLoop
from nanodot.core.tasks import PRTarget, Task, TaskStore
from nanodot.native.daemon import RunnerDaemon

TARGET = PRTarget.parse("thinkflowlab/nanodot#9")
TICKS = 3_000
CADENCE = 300
DAY = 86_400
FLIP_EVERY = 500
COMMIT_EVERY = 1_500


def test_soak_long_run_health(home: Path) -> None:
    clock = FakeClock()
    store = TaskStore(path=home / "nanodot.db", clock=clock)
    activity = ActivityLog(path=home / "nanodot.db")
    github = FakeGitHub(TARGET, required=())  # never completes: no required
    github.set_pr("open", head_sha="s0")
    github.checks["s0"] = []
    github.add_check("ci", FAILURE, sha="s0")
    sink = FakeSink()
    loop = TaskLoop(store, github, sink, activity)
    task = store.create(
        Task(
            target=TARGET, purpose="soak", cadence_seconds=CADENCE,
            digest_interval_seconds=DAY, stale_after_seconds=2 * DAY,
            flaky_alerts=True, next_check_at=0.0,
        )
    )
    daemon = RunnerDaemon(loop, store, clock=clock)

    # Warmup (schema, first tick) before tracing so setup noise stays out.
    daemon.tick()
    clock.advance(CADENCE)
    tracemalloc.start()
    setup_current, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()

    timings: list[float] = []
    try:
        for tick in range(1, TICKS + 1):
            if tick % FLIP_EVERY == 0:
                runs = github.checks[github.head_sha]
                flipped = SUCCESS if runs[-1].conclusion == FAILURE else FAILURE
                github.checks[github.head_sha] = [
                    type(runs[-1])(
                        name=runs[-1].name, status=COMPLETED,
                        conclusion=flipped, sha=github.head_sha,
                    )
                ]
            if tick % COMMIT_EVERY == 0:
                new_sha = f"s{tick // COMMIT_EVERY}"
                github.set_pr("open", head_sha=new_sha)
                github.checks[new_sha] = []
                github.add_check("ci", FAILURE, sha=new_sha)
            start = time.monotonic()
            daemon.tick()
            timings.append(time.monotonic() - start)
            clock.advance(CADENCE)
        end_current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Memory: no steady-state growth, bounded peak.
    assert end_current - setup_current < 1_000_000, (
        f"steady-state growth {(end_current - setup_current) / 1e6:.2f} MB"
    )
    assert peak < 10_000_000, f"traced peak {peak / 1e6:.2f} MB"

    # Latency: the last stretch must not drift from the first.
    head = sum(timings[:200]) / 200
    tail = sum(timings[-200:]) / 200
    assert tail < 3 * head + 0.005, f"drift: first {head*1e3:.2f}ms, last {tail*1e3:.2f}ms"

    # Retention: observations pruned to the cap across the whole run.
    observed = activity.query(task_id=task.id, kinds=("check-observed",), limit=1000)
    assert len(observed) <= OBSERVATION_RETENTION, len(observed)

    # DB growth bounded for a simulated fortnight of full-kit watching.
    size = (home / "nanodot.db").stat().st_size
    assert size < 10_000_000, f"db {size / 1e6:.2f} MB"

    # Functional sanity: all three kinds fired; the watch stayed healthy.
    kinds = [e.kind for e in sink.events]
    simulated_days = TICKS * CADENCE / DAY
    assert kinds.count(DIGEST) >= simulated_days - 2, kinds.count(DIGEST)
    assert FLAKY in kinds and STALE in kinds
    final = store.get(task.id)
    assert final.state.value == "active" and final.next_check_at is not None
    store.close()
    activity.close()

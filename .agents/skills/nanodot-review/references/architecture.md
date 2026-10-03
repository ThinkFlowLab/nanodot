# Snapshot-aware architecture

Verified 2026-10-03 Asia/Shanghai. This is a source map, not a claim that every
listed behavior has been executed or shipped. Refresh exact refs on each review.

## History: the stacked MVP reached main via the integration PR

- PRs #17–#27 merged into stacked feature-branch bases, not main. Their merged
  flags alone never established main availability.
- [PR #28](https://github.com/ThinkFlowLab/nanodot/pull/28) integrated that
  stack into main (merged 2026-10-02 at
  [661f4bae](https://github.com/ThinkFlowLab/nanodot/commit/661f4bae106e9f9718137812a803020b8954acc8)).
- Since then main also carries: npm launcher and packaging (#30, #43 —
  `bin/nanodot.cjs`, uv-manifest, integrity-verified downloads), installation
  and user docs (#31), the activity decision log (#36), adapter admission
  discipline docs (#38), reverse-order teardown (#39, `core/teardown.py`),
  the single-device ownership boundary doc (#40), per-PR CI runs (#44), and
  two systematic bug-scan fix passes (#34, #47).
- Current pin for this map:
  [main 711b45c](https://github.com/ThinkFlowLab/nanodot/commit/711b45c77988df70837bc45126072f4a8d0ac0c2).
  PR bodies and historical safety-validation notes contain older test counts;
  only exact-head runs support current verification claims.

Main declares Python >=3.11, setuptools, pytest>=8, and `nanodot.cli:main`,
plus an npm packaging layer (`package.json`, `bin/nanodot.cjs`, `npm-tests/`)
that bootstraps a managed Python environment.
[CI](https://github.com/ThinkFlowLab/nanodot/blob/711b45c77988df70837bc45126072f4a8d0ac0c2/.github/workflows/ci.yml)
runs per PR once and on pushes to main (#44): Python 3.12, editable dev
install, offline pytest behind dead proxies, then this skill's network-free
unittest suite (also behind the dead proxies). The autouse socket guard in
`tests/conftest.py` additionally rejects direct network calls that do not
honor proxy variables.

The [adapter-seam design](https://github.com/ThinkFlowLab/nanodot/blob/711b45c77988df70837bc45126072f4a8d0ac0c2/docs/design/adapter-seam.md)
is intended architecture. Verify implementation rather than treating it as shipped.

## Implementation map (main 711b45c)

All paths below resolve on main at the pinned tree.

| Boundary | Actual implementation |
| --- | --- |
| Composition | `cli.py` wires stores/ports and validates fixed scope; `_run_runner` holds a lifetime RunnerLease before wiring |
| Snapshot | `ports/github.py` defines CheckRun, RequiredCheck, Snapshot and typed errors; `native/github_client.py` performs GET-only paginated head-pinned reads |
| Deterministic decision | `core/github_eval.py` evaluates required success separately from observed failure; `core/statemachine.py` generates transitions/events with persisted occurrence sequence |
| Durable task loop | `core/tasks.py` owns SQLite task scope/lifecycle; `core/runner.py` reloads scope/state before delivery, handles blockers/backoff, notifies before checkpoint and persists terminal state before optional memory |
| Scheduler control | `native/daemon.py` isolates task failures and checks stop before each task; `native/runner_control.py` uses a lifetime flock and token-specific stop/readiness ownership, never a PID signal; `core/teardown.py` unwinds registered disposers in reverse order on shutdown |
| Delivery | `native/notifier.py` owns SQLite inbox/event-key dedup and best-effort OS popup; persistence is actually here despite broader design wording |
| Safety and optional inference | core permissions/egress/config/redaction/memory plus native secrets/http/inference adapters; `native/http.py` never follows redirects for credential-bearing requests; anonymous mode must not read/send saved credentials; summary failure cannot determine watcher truth |

## Locate tests by symbols

- Evaluator/transitions: `tests/test_github.py`, `test_statemachine.py`
- Store/scope: `test_tasks.py`, `test_scope_lifecycle_safety.py`
- Loop/retry/stop: `test_runner.py`, `test_daemon_resilience.py`,
  `test_runner_control.py`, `test_teardown.py`
- Inbox/replay: `test_notifier.py`, `test_first_use_demo.py`
- Real CLI: `test_cli.py`, `test_public_mode.py`; eight fake-HTTP/real-CLI scenarios
  live in `examples/first_pr_watch.py`
- Safety: `test_secrets.py`, `test_http_transport.py`, `test_inference.py`,
  `test_production_safety.py`, `test_permissions.py`, `test_memory.py`
- Whole flow and boundaries: `test_e2e.py`, `test_offline.py`, `test_scaffold.py`

Use an isolated home, installed declared dependencies and the target's offline
fixture configuration. `tests/conftest.py` blocks in-process sockets
and DNS; dead proxies propagate to subprocesses. This is a tripwire, not an OS
network sandbox. Do not put dead proxies on dependency-install/checkout steps.

# Safety and validation review

These changes build on the complete MVP stack at `32f11f4` (`issue-14`, PR #27).
Main at review time was `82eb79a`, containing only the scaffold and adapter-seam
documentation. Existing PRs remain open; corrections are added without rewriting history.

## Reviewed scope

All eleven open PRs (#17–27) were checked on 2026-09-30. Each had one submitted
review by `hsliuustc0106` at 22:43–22:44 UTC; none had inline review threads or
conversation comments. The patch addresses the functional feedback on the
combined stack:

| PR | Correction |
| --- | --- |
| [17](https://github.com/ThinkFlowLab/nanodot/pull/17#pullrequestreview-5372793871) | Atomic/symlink-safe secret writes, hidden/stdin input, common secret-name routing |
| [18](https://github.com/ThinkFlowLab/nanodot/pull/18#pullrequestreview-5372794516) | Resume scheduling, terminal guards, duplicate-ID domain error |
| [19](https://github.com/ThinkFlowLab/nanodot/pull/19#pullrequestreview-5372795262) | Complete paginated checks/statuses, required metadata, secondary throttling, early failure outcome |
| [20](https://github.com/ThinkFlowLab/nanodot/pull/20#pullrequestreview-5372795847) | New-commit failure reset and durable occurrence identity |
| [21](https://github.com/ThinkFlowLab/nanodot/pull/21#pullrequestreview-5372796525) | Recovery without scheduler test workarounds, per-task unexpected-failure isolation |
| [22](https://github.com/ThinkFlowLab/nanodot/pull/22#pullrequestreview-5372797580) | Notification text passed as AppleScript arguments; recurring failures delivered once per occurrence |
| [23](https://github.com/ThinkFlowLab/nanodot/pull/23#pullrequestreview-5372798249) | Safe runner ownership/start/stop controls, lightweight read-only CLI wiring |
| [24](https://github.com/ThinkFlowLab/nanodot/pull/24#pullrequestreview-5372799026) | Factory redaction, nested egress scrubbing, bounded optional summaries, fenced JSON parsing |
| [25](https://github.com/ThinkFlowLab/nanodot/pull/25#pullrequestreview-5372799810) | Real memory CLI safety/tombstones, enforced proposal expiry, secure deletion, observation failure isolation |
| [26](https://github.com/ThinkFlowLab/nanodot/pull/26#pullrequestreview-5372800905) | Reject unsupported modes, unconditional external-write denial, structural scope revocation, expiry-aware approval/read |
| [27](https://github.com/ThinkFlowLab/nanodot/pull/27#pullrequestreview-5372801573) | Unmasked lifecycle regressions, real CLI entrypoint tests, stronger offline tripwire, test-step-only CI proxy |

#24's missing production provider wiring was already addressed in #25; this
patch preserves it and fixes its redactor. Unsupported custom `--stop`/`--notify`
text is now rejected instead of being saved and ignored.

Small nonfunctional suggestions are intentionally not a new architecture
project: stores still use SQLite's normal busy timeout and do not enable WAL;
permanent secret caching is avoided so rotation stays visible; the small
no-secret fallback objects are unchanged. Secure deletion is explicitly enabled
rather than relying on the host SQLite build default.

## Verification boundaries

Tests use real core/SQLite and fake transports, including production CLI and
provider factories. No real provider calls, credentials, authorization grants,
notifications to a user, CI workflow runs, or repository write operations are
needed. Native macOS rendering is not exercised on Linux; the exact
`osascript` argv boundary is tested. This is not a paid or live end-to-end test.

Required-check completion deliberately fails closed for unavailable/unsupported
metadata. It is stricter than GitHub's merge gate and is not a mergeability
promise. See README limitations. Tests establish the behavior with mocks;
real repository permission differences remain an operational consideration.

## Integration recommendation

Review the existing dependency stack in order (#17 → #27). Each corrected
branch preserves its original head as a parent and incorporates the corrected
predecessor as an additional parent, keeping updates fast-forward and each PR
focused on its own feature. No PR is merged and no branch history is rewritten.
Do not retarget later features directly onto scaffold-only main without their
dependencies.

Publication commits carry `[skip ci]`, which suppresses the repository's only
workflow triggers, push and pull_request. The repository is public and uses
standard `ubuntu-latest`, whose Actions compute is free. This work does not
disable workflows, purchase credits, rerun checks, merge PRs, or enable
auto-merge. Remote CI is intentionally not evidence for these commits.

## Local verification result

On Python 3.12, the original correction package passed **386 tests**; the
published stack adds two production regressions and passes **388 tests** with dead proxies plus
in-process socket/DNS rejection. Compile checks, dependency checks, CLI help,
and `git diff --check` pass. Both the fixes-only patch on `32f11f4` and the
full-stack patch on `82eb79a` apply cleanly and produce the same source tree;
the reconstructed main-based correction-package tree also passes its full suite.
Each intermediate feature branch was independently tested before publication.

For comparison, the unmodified `32f11f4` baseline passed 107 of 108 tests on this
host: its deletion byte-check failed because secure deletion depended on the
SQLite build default. The final code sets it explicitly. This local result is
independent of the reviews' historical test claims. Remote CI was not run.

# Contributing to nanodot

Read the [README](README.md) for setup, supported behavior, and the existing
[parallel-work protocol](README.md#parallel-work-claim-before-you-start).
Check open PRs and recent claims before starting to avoid duplicate work.

## Before implementation or a PR

Use the [contributor precheck skill](.agents/skills/precheck-pr/SKILL.md), starting
with its [human contributor-intent gate](.agents/skills/precheck-pr/SKILL.md#human-contributor-intent-gate),
before implementation and before submitting a PR, including a draft. The human
must explicitly identify the outcome, highest-leverage variable, how the work
moves it rather than creating busywork, customer and actual need, then whether a
PR is needed at all. Reuse traceable explicit answers; ask only for missing ones
and wait. Never invent human answers. Bug fixes need linked observed failure
evidence. Keep private source material private.

This gate governs contributor preparation, not independent read-only reviews.
The existing [nanodot-review skill](.agents/skills/nanodot-review/SKILL.md) and its
automated batch-selection rules are unchanged.

## Validation

Self-review the complete diff and run checks appropriate to the changed behavior.
Follow [the README's offline test instructions](README.md#test-offline) and
[CI](.github/workflows/ci.yml). For changes affecting the independent review skill,
run its documented network-free validation as well.

Documentation-only changes need link, example, and factual-claim checks, without
unrelated runtime or model execution. Report exact commands and results, and
name checks not run with the reason. A missing check is not a pass. Include the
purpose, scope, observed bug evidence when applicable, and validation in the PR;
keep incomplete work in draft and rerun affected checks after changes.

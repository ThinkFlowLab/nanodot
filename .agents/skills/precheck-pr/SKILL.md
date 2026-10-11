---
name: precheck-pr
description: Prepare a nanodot contribution before implementation or PR submission by checking human intent, necessity, the diff, and relevant validation. Not for independent read-only PR review or automated review selection.
---

# nanodot contributor precheck

Read [CONTRIBUTING.md](../../../CONTRIBUTING.md), [README.md](../../../README.md),
and applicable checkout instructions. Start with the human gate below. This skill
prepares a local contributor report; it does not authorize edits, commits, pushes,
posts, paid execution, or changes to review status.

## Human contributor-intent gate

Complete this gate with the human contributor before implementation and before
submitting a PR (including a draft). At pre-submit self-review, verify the answers
still describe the actual diff; ask again only where the scope or evidence changed.
This is a contributor preparation step, not a prerequisite for an independent
read-only review of someone else's PR, and it does not change automated review
selection or authorize any external action.

Use explicit human answers already present in the task, issue, or discussion,
keeping a traceable reference to their source. Ask only for missing or ambiguous
answers, then wait for the human; do not infer, invent, or answer on their behalf:

1. What outcome are you trying to achieve?
2. What is the highest-leverage variable for that outcome?
3. How will this work move that variable rather than create busywork?
4. Who is the customer or user, and what do they actually need?

After those answers are clear, ask the human: **Is a PR needed at all to achieve
this outcome?** Reuse an explicit, still-applicable human decision if already
recorded. If the answer is no or remains unclear, stop implementation/submission
and clarify the next step instead of manufacturing a change to justify a PR.

For a bug fix, also link actual observed failure evidence, such as a minimal
reproduction with its observed result, a failing regression test, or a relevant
trace. Separate observed behavior from an untested diagnosis; a hypothetical
failure is not verified evidence. If the evidence is missing, ask for it or obtain
it within the authorized scope before proceeding.

Keep source references in the working task and include only audience-appropriate,
authorized summaries or links in the PR. Do not copy private conversations,
customer data, or credentials into a public repository. Missing human answers
remain a blocker; an agent-written rationale or completed checklist cannot
substitute for them.

## Pre-submit self-review

- Record the actual base and head commits and review the complete diff from their
  merge base, plus relevant uncommitted changes. Disclose any unavailable base.
- Confirm the diff matches the human's stated need. Remove unrelated changes only
  when authorized; report scope problems rather than inventing a rationale.
- Check changed behavior, callers, tests, and documentation against the README
  and applicable design contracts. Bug fixes need regression coverage tied to
  the observed failure; avoid speculative correctness claims.
- Run relevant checks from [the contributor guide](../../../CONTRIBUTING.md#validation).
  For documentation-only changes, verify links, examples, and claims without
  unrelated runtime work. Report exact commands and passed, failed, or unrun checks.
- Report the intent source references, reviewed scope, findings, evidence, and
  remaining gaps. Do not fill in the human's answers or check boxes for them.
  Do not submit with missing intent answers; keep an existing PR draft while
  blocking self-review gaps remain.

For an independent review, use [nanodot-review](../nanodot-review/SKILL.md).
Its review routes and batch-selection policy remain unchanged.

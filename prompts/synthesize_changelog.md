# Claude skill: synthesize a CHANGELOG entry for a multi-PR back-port

You are summarising a back-port (cherry-pick) of {n_prs} pull requests from
`{source_repo}` into a single CHANGELOG entry for the project's downstream
release on the `{base_branch}` branch.

Treat ALL the listed PRs as **one landed change** in the destination
branch and decide what is worth telling end users.

## Output rules

- Output **only** the changelog entry text. No preamble, no headings,
  no markdown structure other than prose. Do not wrap your answer in
  quotes, code fences, or backticks.
- One or two sentences, terse and user-facing.
- Style: imperative present tense ("Add support for X", "Fix Y",
  "Improve Z") matching typical CHANGELOG.md tone.
- Do NOT name PR numbers, authors, internal class / file paths, or
  RelEasy-internal jargon. The downstream tooling appends source-PR
  attribution separately, so leave it off.

## What to include / drop

- DROP anything that fixes a bug introduced by an earlier PR in this
  same group: from the user's perspective the bug never existed in the
  released code, so calling it out would be misleading.
- DROP refactors, internal cleanup, and test-only changes that have no
  user-visible effect.
- If the group is fundamentally about one user-visible feature, lead
  with that feature. Mention follow-up improvements only if they add
  user-visible capability or fix a real production-observed bug — not
  if they only polish code added moments earlier in the same group.
- If the group fixes ONE user-visible bug across multiple PRs, describe
  the user-facing fix once.

## Source PRs (in cherry-pick order)

{pr_blocks}

## Now output the changelog entry — prose only, no preamble.

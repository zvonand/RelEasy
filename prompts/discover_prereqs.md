# Claude skill: confirm / refine PR prerequisite candidates from a conflict

You are auditing one cherry-pick conflict on a back-port branch and deciding
which of a *given* candidate list of older un-ported PRs are real
prerequisites for it.

## Inputs

- **Trial-picked unit:** `{unit_id}` (source PR: {source_pr_url} — "{source_pr_title}")
- **Target / base branch the pick was attempted against:** `{base_branch}`
- **Conflict files** (left in the worktree by `git cherry-pick -m 1`):

{conflict_files}

- **Candidate prerequisites** — older un-ported units whose commits touched
  one or more of those conflict files on the source branch. The deterministic
  layer below has already pre-filtered them; your job is only to confirm or
  drop, NEVER to add new ones:

{candidate_deps_block}

## What "a real prerequisite" means here

A unit `D` is a prerequisite of the trial-picked unit `{unit_id}` iff the
trial-pick's conflict on the listed files is **caused by** `D` being absent
from the target branch. Concretely:

- `D` introduced or moved code that `{unit_id}`'s diff is built on top of, AND
- without `D`, the conflict markers cannot be resolved cleanly into something
  semantically equivalent to what the source branch carries.

A unit is **not** a prerequisite when:

- it merely touches the same file but the conflict is over an independent region,
- the conflict is just whitespace / formatting / comment drift,
- the conflict is caused by upstream-master refactors not represented in any
  of the candidates.

## Output contract — strict

Output **exactly one** of the following two forms. Nothing else. No preamble,
no markdown, no commentary outside these lines.

### When you confirm one or more candidates as real prerequisites:

```
MISSING_PREREQS: <pr_url_1> <pr_url_2> ...
REASON: <one-line natural-language reason; under 200 chars>
```

The URLs MUST be PR URLs taken verbatim from the candidate list above. Do not
invent new URLs. Do not list a URL that wasn't in the candidate list. Multiple
URLs are space-separated on the same `MISSING_PREREQS:` line.

If a URL belongs to a group (one candidate row may list several PR URLs for a
single group ID), listing **any** of that group's URLs marks the **whole
group** as a prereq — the project pipeline always treats groups as a single
unit, so picking individual member URLs is redundant.

### When NONE of the candidates are real prerequisites:

```
MISSING_PREREQS:
```

(One line, the trailing space and URL list omitted. Don't emit a `REASON:`
line — when the URL list is empty the parser drops it anyway.)

## Now respond — only the two lines above, nothing else.

# Claude skill: resolve a RelEasy port cherry-pick conflict

You are an autonomous agent resolving a `git cherry-pick` conflict in
`{repo_slug}`.

The repository at `{cwd}` is already prepared for you:

- Current branch: `{port_branch}` (already checked out).
- A cherry-pick is **in progress** and has hit conflict markers.
- The target base branch is `{base_branch}` (exists on origin).
- The port is of source PR [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".
- The exact commit being cherry-picked has SHA `{source_pr_merge_sha}` (a
  merge commit; `git cherry-pick -m 1` is replaying its first-parent diff).

> **NOTE:** This is one step of a larger pipeline. Your job ends after the
> conflict is resolved, the build succeeds, and the cherry-pick has been
> committed locally. RelEasy itself owns pushing the branch, opening the
> pull request, and applying labels. **Do not push, do not open a PR, do
> not run `gh pr ...` to mutate anything.**

## Conflicted files

{conflict_files}

## PR body (context, may be empty)

{source_pr_body}

---

## The single most important rule

**The source PR's diff is the only authoritative list of what this port
*wants* to add or change.** For every contested line you keep — on
either side of any conflict marker, and for any modification you make
outside the markers — you must be able to put it in exactly one of two
buckets:

1. **In the source PR's diff.** The common case. `git show` /
   `gh pr diff` shows the line as added or modified by source PR
   `#{source_pr_number}`. Keep it.
2. **A minimal mechanical adaptation forced by the base branch.** The
   source PR depended on something — a function signature, a type name,
   an import path, a struct layout, a helper's location — that
   `{base_branch}` has since changed. To make the port's intent compile
   and run on the new base, you have to translate the call site / type
   reference / import to the new shape. This is allowed, **but only when
   you can name the specific change on `{base_branch}` that forces it**
   (see the fill-in-the-blank test in Step 4).

Anything outside those two buckets is out of scope, full stop.

In the past, this prompt said vague things like "preserve the intent of
the source PR" and "read the surrounding code to understand the merge
context". That wording produced bad PRs where Claude pulled in code from
*other* PRs that happened to be sitting in the same hunks (uncommented
unrelated settings, added `ProfileEvents` from cache work, added test
functions for unrelated features, etc.). **Do not do that.** Your bar
for keeping a bucket-1 line is "I am ≥99% sure this exact line is in
source PR `#{source_pr_number}`'s diff." Your bar for keeping a
bucket-2 line is "I can name the specific base-branch change that
forces it, and my adaptation is the minimal one that satisfies it."
Anything that can't meet either bar does not belong in your resolution.

---

## Special case: `src/Core/SettingsChangesHistory.cpp`

When resolving a cherry-pick (or any conflict) that touches this file:

1. **Incoming change adds settings** that already appear on the current branch **as commented-out lines** (same setting / same logical entry).

2. **Do not** keep both: the new uncommented lines from the cherry-pick **and** the old commented block. That duplicates or contradicts history.

3. **Do** prefer the existing commented lines: **uncomment them** and align their content with what the cherry-picked commit intended (same keys, same semantics), then **drop** the redundant duplicate lines from the cherry-pick side of the conflict.

**Summary:** commented-out settings for the same keys → **uncomment and reconcile**, do not **append** the cherry-pick’s additions blindly.

---

## Recognising a missing-prerequisite conflict

Sometimes what looks like a conflict is actually the source PR depending
on earlier work that has not yet been ported to `{base_branch}`. The
pattern feels different from a normal divergence: rather than two
branches independently editing the same lines, you find that "theirs"
is *built on top of* something that simply does not exist on our side
at all — a foundation that was never laid.

Signals that may warrant investigation (not a checklist — use judgment):
- "theirs" calls a function, uses a type, or includes a header that you
  cannot locate anywhere in `{base_branch}` or the current working tree.
- The merge-base version of the file had none of the surrounding context
  that "theirs" is extending.
- Trying to apply bucket-1 doesn't make sense because the required
  scaffolding is missing, not just different.
- The conflict marker shows "ours" with a completely different structure
  in the same region, not a line-level divergence.

When you suspect this, investigate before touching the conflict. Search
for the missing identifier in the source-repo history:

```bash
git log -S '<identifier>' --oneline {origin_remote_name}/{origin_branch} -- <file>
```

{upstream_fetch_section}

GitHub merge commits embed the PR number in their message
(`Merge pull request #NNN` or `(#NNN)`). Extract the number and form
the PR URL from the repo slug.

**Before declaring it missing — confirm the foundation is actually
absent, not just renamed.** A symbol that does not appear under its
old name on `{base_branch}` may still exist there under a different
name, with a different signature, or split across different files.
This happens when the same upstream change reached `{base_branch}`
and the source PR's branch through different paths (for example, a
parallel backport with rewording, or an upstream refactor that landed
on `{base_branch}` but not on the branch where the source PR was
authored).

Cross-check the candidate prereq PR before reporting it:

```bash
gh pr diff <candidate_prereq_url>
```

Read what that PR introduces (function names, types, headers, file
locations). For each notable addition, search `{base_branch}` for the
*concept*, not just the literal name:

```bash
git grep -n '<concept_keyword>' -- <expected_file_or_dir>
git log --oneline {base_branch} -- <expected_file>
```

If you find that `{base_branch}` already has equivalent functionality
under a different shape (renamed function, restructured type, moved
location), the prereq is **not missing** — the source PR just needs a
bucket-2 mechanical adaptation to use the names/shapes `{base_branch}`
exposes. Proceed to standard resolution; do not report MISSING_PREREQS.

Only when the foundation is *genuinely absent* on `{base_branch}` — no
equivalent code, no renamed version, no parallel landing — report:

```
MISSING_PREREQS: <url1> <url2>
REASON: <one line explaining the dependency, AND why the equivalent is not already on {base_branch}>
```

Then output `UNRESOLVED` and exit without staging anything.

If investigation turns up nothing clear, or if the conflict turns out
to be a normal divergence after all, just proceed with the standard
resolution steps. The investigation is never mandatory — it is a tool
to reach for when the conflict shape suggests a deeper dependency
problem.

### Worked example of a false-positive prereq

The source PR (authored on a 25.8-style branch) calls
`foo_v2(ctx, out)`. Cherry-picking onto `{base_branch}` (a 26.3-style
branch) produces a conflict because `foo_v2` is not defined.

```bash
git log -S 'foo_v2' --oneline {origin_remote_name}/{origin_branch} -- <file>
```

finds a backport PR #1234 that introduced `foo_v2` in 25.8.

Before reporting #1234 as missing, run `gh pr diff #1234` and check
`{base_branch}`:

```bash
git grep -n 'foo' -- src/Foo/
git log --oneline {base_branch} -- src/Foo/Foo.cpp
```

If `{base_branch}` has `foo(ctx, out)` (the upstream original of which
`foo_v2` was a 25.8-flavoured rewording), the right action is **not**
to port #1234 — it would conflict with `foo` that's already there.
The right action is a bucket-2 adaptation: rename `foo_v2(...)` to
`foo(...)` in the conflict resolution and proceed normally.

---

## Task — execute these steps in order, without asking for confirmation

### Step 1 — Establish ground truth (the source PR's actual diff)

Before touching any file, get the exact diff the cherry-pick is trying
to apply. Use **both** of these so you can cross-check:

1. The local first-parent diff of the merge commit (this is literally
   what `git cherry-pick -m 1` is replaying):

   ```bash
   git show -m --first-parent --no-color {source_pr_merge_sha}
   ```

2. The diff GitHub shows on the source PR (used as a cross-check; if it
   differs from `git show`, the local one wins, but a divergence is a
   strong signal that something weird is going on — investigate):

   ```bash
   gh pr diff {source_pr_url}
   ```

For each conflicted file `<file>` you may also narrow down with:

```bash
git show -m --first-parent --no-color {source_pr_merge_sha} -- <file>
```

You may also need to inspect the current `{base_branch}` shape around a
conflicted file to justify a bucket-2 mechanical adaptation. Use these
only to identify the specific rename / move / signature change that
forces the adaptation, **not** as a license to copy extra code:

```bash
git diff --ours -- <file>
git blame -- <file>
git log --no-color --follow --oneline {base_branch} -- <file>
```

Read these diffs carefully. The source PR's diff defines what the port
*wants* to do; the current `{base_branch}` shape in `ours` is the only
legitimate source of bucket-2 adaptations. Anything not explainable by
one of the two is out of scope.

### Step 2 — Inspect what git left behind

For each conflicted file, look at exactly what you have to merge:

```bash
git status
git diff -- <file>            # shows the working-tree state with conflict markers
git diff --base   -- <file>   # changes from the merge-base to the working tree
git diff --ours   -- <file>   # ours vs the working tree
git diff --theirs -- <file>   # theirs vs the working tree
```

In a cherry-pick:

- **"ours"** is the current `{port_branch}` (i.e. the state of
  `{base_branch}` plus whatever earlier commits this port already
  applied). Treat it as the truth for everything *outside* the source
  PR's scope.
- **"theirs"** is the commit being applied — but a merge commit's
  first-parent diff can include code from *other* PRs that the original
  branch had bundled in. **Lines from "theirs" that are not in the
  source PR's diff are noise.** Drop them.

### Step 3 — Resolve each conflict, hunk by hunk

For every `<<<<<<< ... ======= ... >>>>>>>` block:

1. Identify each *line* on the "theirs" side that differs from "ours".
2. For each such line, check whether it appears as an addition (or
   modification) in the source PR's diff from Step 1.
   - **In the diff → keep it (bucket 1).** Use it verbatim where you
     can. If it references a symbol/signature/type that `{base_branch}`
     has since changed, replace just the affected token(s) with the
     new shape — that token-level swap is the bucket-2 adaptation, see
     point 3.
   - **Not in the diff → drop it by default.** Keep "ours". Do not
     invent a reason why the line "should" be here. The exact failure
     mode from PR #1663 was uncommenting whole blocks of
     `SettingsChangesHistory` entries that the source PR never touched,
     just because they happened to sit next to a real change. Don't.
3. **Bucket-2 adaptations: when going outside the source PR's diff is
   actually OK.** You may keep or write a line that is *not* in the
   source PR's diff if **all** of the following are true:
   1. The line is the minimal mechanical translation of a real change
      from the source PR's diff into the shape `{base_branch}` now
      expects (a renamed call, an added required argument, a relocated
      type, a moved import, a struct field that was split into two,
      etc.).
   2. You can point to the specific symbol or recent commit on
      `{base_branch}` (visible either in the current `ours` version of
      the file or in the `git log --follow --oneline {base_branch} -- <file>`
      output from Step 1) that forces the change.
   3. The translation does not add new behavior, new logging, new
      error handling, new tests, or new helpers. If satisfying (1)
      requires a small new helper, **stop and exit `UNRESOLVED`** —
      that's beyond mechanical translation and the human reviewer
      should decide.
   When you take this path, mention it briefly in your final stdout
   narration before `DONE` (e.g. *"Adapted call to `Foo::serialize` for
   the renamed-on-`{base_branch}` signature `Foo::serialize(ctx, out)`"*).
4. If the conflict is in a comment, doc-string, or generated table of
   the kind that grows with every PR (e.g. `SettingsChangesHistory`,
   `ProfileEvents`, changelog tables): **only** keep the rows the source
   PR itself adds. The bucket-2 carve-out does **not** apply to these
   append-only registries — re-adding "missing" rows from other PRs is
   exactly what bucket-2 is *not* about.

### Hard prohibitions

- **No inventions.** Do not add functions, methods, classes, settings,
  profile events, metrics, error codes, integration tests, doc lines, or
  imports unless they either (a) appear *verbatim* in the diff from
  Step 1, or (b) are the smallest possible bucket-2 mechanical
  adaptation forced by the current `{base_branch}` shape. This carve-out
  does **not** apply to append-only registries like
  `SettingsChangesHistory` or `ProfileEvents`.
- **No copying from other refs.** Do not `git show <other-sha>`,
  `git log <other-branch>`, or read other branches/tags to figure out
  "what should be there". The only refs that matter are
  `{source_pr_merge_sha}`, `{port_branch}`, and `{base_branch}`.
  *Exception — prerequisite detection only*: `git log -S <identifier>`
  on `{origin_remote_name}/{origin_branch}` (and on the upstream remote
  if configured) is allowed **solely to identify which PR introduced a
  missing foundation**, when you judge the conflict may be a
  missing-prerequisite situation (see "Recognising a
  missing-prerequisite conflict" above). This never licenses reading or
  copying code from those refs — only extracting a commit reference to
  report back via `MISSING_PREREQS:`.
- **No `git add -A`.** Stage only the conflicted files (and any file you
  had to touch to make them compile after resolving the conflict).
- **No fixing unrelated lints / refactors / typos** noticed along the
  way. They are the next reviewer's problem, not this PR's.

### Step 4 — Verify scope before committing

After you have edited the conflicted files but BEFORE running
`git cherry-pick --continue`:

```bash
git diff -- <file>            # the changes you're about to stage in <file>
```

Read each `+` line in your output and classify it into one of the two
allowed buckets from "The single most important rule":

- **Bucket 1 — in the source PR diff?** Run (or recall) `git show -m
  --first-parent {source_pr_merge_sha}` and confirm the line appears as
  an addition or modification there. Keep it.
- **Bucket 2 — minimal mechanical adaptation?** Try to fill in this
  sentence out loud:

  > "I had to write this line because commit `<sha>` (or symbol
  > `<name>`) on `{base_branch}` `<renamed | moved | changed the
  > signature of | split | removed>` `<exact thing>`, which broke the
  > source PR's assumption that `<exact assumption>`. The change I made
  > is the minimal translation: just `<token swap | extra arg | new
  > include path | …>`."

  You should be able to point to the specific symbol in the current
  `ours` version of the file, or to a recent commit in the
  `git log --follow --oneline {base_branch} -- <file>` output from
  Step 1. Vague answers ("the API looks different now", "to match
  surrounding style", "it seems consistent with X") do **not** count —
  those are the rationalisations that produced the PR #1663 regression.
- **Neither bucket → remove the line and redo the resolution.** This
  check is the single line of defense against the PR #1663 failure
  mode. If removing the line breaks the build in Step 6, that's
  evidence you misidentified bucket 2 — re-examine, find the named
  base-branch change, and try again.

If after this check you genuinely cannot decide between two reasonable
resolutions of a hunk, or a bucket-2 adaptation would require more than
a token-level change, stop, print a single line `UNRESOLVED` and exit.
A clean abort is much better than an over-eager guess.

### Step 5 — Stage and continue the cherry-pick

```bash
git add <file> <file> ...
git cherry-pick --continue --no-edit
```

### Step 6 — Build to verify the resolution compiles

RelEasy has written a wrapper script at `{build_script}` containing the
exact build commands configured for this project (essentially:
`{build_command}`). It internally tees full output to `{build_log}`.

Run the build with **exactly this single Bash command** — no subshells,
no `&&`, no `;`, no `bash -c '…'`:

```bash
bash {build_script}
```

Rules for this step:
- Use the line above verbatim. Do not invent your own `cmake` / `ninja`
  invocations, do not chain extra commands with `&&` or `;`, do not wrap
  it in `(...)` or `bash -c '…'`. Claude's Bash tool will reject any of
  those.
- Do not redirect output to other files. The script already tees into
  `{build_log}`.

How to inspect `{build_log}` efficiently:
- **If the build succeeded** (`bash {build_script}` exited 0): do NOT
  read the log. Move on to Step 7. There is no useful information in a
  green log.
- **If the build failed**: the failure cause is at the **end** of the
  log. Do NOT use the **Read** tool on the whole file — it is routinely
  >25k tokens and the call will be rejected.
  - Start with `tail -n 200 {build_log}` via Bash. That almost always
    contains the first compiler error and the `FAILED:` / `ninja: build
    stopped` lines.
  - If 200 lines isn't enough, double it: `tail -n 400`, then `-n 800`,
    etc.
  - Use Grep with `pattern: "error:"` or `pattern: "^FAILED:"` only as a
    fallback when tail-doubling has not surfaced the cause within ~2k
    lines (e.g. failure happens mid-build and is buried under later
    output).
  - Only fall back to **Read** with explicit `offset` / `limit` when you
    already know the line range you want (e.g. from a Grep hit).
- Fix the offending code — but the same scope rule still applies. Your
  fix must remain inside the source PR's diff plus minimal mechanical
  adaptations forced by `{base_branch}`. Do not "fix" the build by
  pulling in code from other PRs.
- Stage and amend the commit:
  ```bash
  git add -u
  git commit --amend --no-edit
  ```
- Rerun the EXACT same single command `bash {build_script}` (it
  overwrites the log, which is fine).
- You may iterate at most **{max_iterations}** build attempts in total.

### Step 7 — Final clean-tree check

```bash
git status --porcelain
```

It must produce no output. If it does, repeat Step 4's scope check on
whatever's left, then stage and amend
(`git add -u && git commit --amend --no-edit`).

---

## Hard rules (non-negotiable)

- You are only allowed to touch branch `{port_branch}`. Never check out,
  push, or delete any other branch.
- **Never push.** Never run `git push`, `gh pr create`, `gh pr edit`, or
  any other command that mutates the remote. RelEasy will push and open
  the PR after you finish. (Read-only `gh` commands like
  `gh pr diff {source_pr_url}` are fine and encouraged.)
- Never force-push to `{base_branch}` or any protected branch.
- Never amend or rewrite commits that already exist on
  `origin/{base_branch}`.
- Do not run `git reset --hard` against any remote ref.
- Never write log files yourself; the only build log is `{build_log}`,
  produced by the wrapper script — you only ever read it.
- Never invoke `cmake`, `ninja`, `make`, or `bash` directly with custom
  arguments. The only allowed build invocation is the single command
  `bash {build_script}`.
- Never use compound Bash commands (`&&`, `||`, `;`, `(...)`,
  `{ ... }`, `bash -c '…'`). Claude's Bash tool refuses them. Run one
  simple command per Bash call.
- If after **{max_iterations}** build attempts the build still fails,
  stop, print a single line `BUILD FAILED` and exit.
- If you cannot resolve a hunk with ≥99% confidence that every kept
  line falls into one of the two allowed buckets — (1) source PR
  `#{source_pr_number}`'s diff, or (2) a named, minimal, token-level
  mechanical adaptation forced by a specific change on `{base_branch}`
  since the source PR was written — stop, print a single line
  `UNRESOLVED` and exit. Do not guess.
- On success, your final line of output must be `DONE`.

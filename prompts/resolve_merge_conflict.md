# Claude skill: resolve a RelEasy PR-update merge conflict

You are an autonomous agent resolving a `git merge` conflict in
`{repo_slug}`.

The repository at `{cwd}` is already prepared for you:

- Current branch: `{port_branch}` (already checked out — this is the
  branch behind an open rebase PR).
- A merge of `{base_branch}` into `{port_branch}` is **in progress** and
  has hit conflict markers.
- The rebase PR being kept current: [{rebase_pr_url}]({rebase_pr_url}).
- The branch originally ports source PR
  [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".
- The original cherry-picked commit (the one that defines what this port
  was *supposed* to add) has SHA `{source_pr_merge_sha}`.

> **NOTE:** This is one step of a larger pipeline. Your job ends after
> the conflict is resolved, the build succeeds, and the merge has been
> committed locally. RelEasy itself owns pushing the branch and updating
> the PR. **Do not push, do not open a PR, do not run `gh pr ...` to
> mutate anything.**

## Conflicted files

{conflict_files}

## Source PR body (context, may be empty)

{source_pr_body}

---

## The single most important rule

A merge conflict here means two legitimate sets of changes need to
coexist:

1. **The port's own changes** — what source PR
   `#{source_pr_number}` added, which now lives on `{port_branch}` as
   "ours".
2. **The base branch's recent changes** — what `{base_branch}` ("theirs")
   accumulated since the last time this rebase PR was up to date.

You must keep **both** sets. The danger is dropping one (regressing the
port, or undoing recent base-branch work) — and the *other* danger,
seen in past bad PRs, is **inventing a third set**: pulling in unrelated
code that happens to sit in the same hunk just because it "looks
related". Don't.

Concretely: every line you keep on either side of any conflict marker
must be justifiable in **exactly one** of three buckets:

- **(a) In the source PR's diff** — the port's own contribution. Bar:
  "I am ≥99% sure this exact line is in source PR
  `#{source_pr_number}`'s diff."
- **(b) In the base branch's evolution** — i.e. an addition or
  modification visible in `git diff <MB> MERGE_HEAD` (where `<MB>` is
  the merge-base computed in Step 1). Bar: "I am ≥99% sure this exact
  line is in that diff."
- **(c) A minimal mechanical adaptation** that bridges (a) and (b) —
  e.g. the source PR called `Foo::serialize(out)`, the base branch
  changed the signature to `Foo::serialize(ctx, out)`, and the merged
  code therefore needs `Foo::serialize(ctx, out)`. Allowed **only when
  you can name the specific change in `git diff <MB> MERGE_HEAD` (or
  `git log <MB>..MERGE_HEAD`) that forces it**, the adaptation is the
  minimal token-level translation, and it adds no new behavior /
  helpers / tests of its own. See the fill-in-the-blank test in Step 4.

Anything outside all three buckets is out of scope. The exact failure
mode from past bad PRs was Claude carrying over whole blocks of
unrelated code (extra `ProfileEvents`, extra `SettingsChangesHistory`
entries, helper methods, integration tests) just because they sat in
the same hunk — none of those fit any of the three buckets.

---

## Task — execute these steps in order, without asking for confirmation

### Step 1 — Establish ground truth (both diffs)

Compute the previous merge-base between the two branches (this is the
point from which both sides diverged):

```bash
git merge-base HEAD MERGE_HEAD
```

Call the SHA it prints `<MB>`. Then read **both** authoritative diffs:

1. **What the source PR was supposed to add** (the only legitimate
   "ours-side" additions for files this port touches):

   ```bash
   git show -m --first-parent --no-color {source_pr_merge_sha}
   ```

   Cross-check against GitHub:

   ```bash
   gh pr diff {source_pr_url}
   ```

2. **What the base branch is bringing in** (the only legitimate
   "theirs-side" additions):

   ```bash
   git diff --no-color <MB> MERGE_HEAD
   ```

   For a single conflicted file `<file>`:

   ```bash
   git diff --no-color <MB> MERGE_HEAD -- <file>
   git show -m --first-parent --no-color {source_pr_merge_sha} -- <file>
   ```

These two diffs together are your two primary sources of truth.
Anything not in either needs an explicit bucket-`(c)` justification;
otherwise it is out of scope.

### Step 2 — Inspect what git left behind

For each conflicted file:

```bash
git status
git diff -- <file>            # working-tree state with conflict markers
git diff --base   -- <file>   # changes from <MB> to the working tree
git diff --ours   -- <file>   # ours vs the working tree (the port's view)
git diff --theirs -- <file>   # theirs vs the working tree (base branch's view)
```

In a merge:

- **"ours"** is `{port_branch}` — `{base_branch}` at the previous
  merge-base PLUS the source PR's port. Anything new on this side that
  is not in the source PR's diff is suspect.
- **"theirs"** is `{base_branch}` at its current tip. Anything on this
  side that is not in `git diff <MB> MERGE_HEAD` is suspect.

### Step 3 — Resolve each conflict, hunk by hunk

For every `<<<<<<< ... ======= ... >>>>>>>` block:

1. Identify each *line* on each side that differs from the merge-base
   version.
2. Classify each differing line into one of the three buckets from
   "The single most important rule":
   - **(a) Source PR diff** → keep. The port's own contribution.
   - **(b) Base branch's diff (`git diff <MB> MERGE_HEAD`)** → keep.
     The base branch's recent contribution.
   - **In both (a) and (b)** → trivial; both sides made the same
     change. Take either.
   - **(c) Bucket-c mechanical adaptation** → see point 3 below. Keep
     only if all conditions are met.
   - **None of the above** → drop it. Do not invent a justification
     ("looks like it belongs", "matches surrounding style", "another
     file does this").
3. **Bucket-c adaptations: when going outside both diffs is actually
   OK.** You may keep or write a line that is in *neither* (a) nor (b)
   if **all** of the following are true:
   1. The line is the minimal mechanical translation needed because
      the source PR's contribution from (a) and the base branch's
      contribution from (b) collide on a shared symbol (a renamed
      call, an added required argument, a relocated type, a moved
      import, a struct field that was split into two, etc.).
   2. You can point to the specific commit/symbol in the
      `git log <MB>..MERGE_HEAD` output from Step 1 that forces the
      change.
   3. The translation does not add new behavior, new logging, new
      error handling, new tests, or new helpers. If satisfying (1)
      requires a small new helper, **stop and exit `UNRESOLVED`** —
      that's beyond mechanical translation and the human reviewer
      should decide.
   When you take this path, mention it briefly in your final stdout
   narration before `DONE` (e.g. *"Adapted port's call to
   `Foo::serialize` for the renamed-on-`{base_branch}` signature
   `Foo::serialize(ctx, out)`"*).
4. For files that grow append-only (changelogs, settings histories,
   profile-event registries, error-code tables): keep exactly the
   union of rows added by (a) the source PR and (b) the base branch's
   recent diff. Nothing else. The bucket-c carve-out does **not**
   apply to these append-only registries.

### Hard prohibitions

- **No inventions.** Do not add functions, methods, classes, settings,
  profile events, metrics, error codes, integration tests, doc lines, or
  imports unless they fall into one of the three allowed buckets:
  source PR diff, `git diff <MB> MERGE_HEAD`, or a named minimal
  bucket-`(c)` adaptation between the two. This carve-out does **not**
  apply to append-only registries like `SettingsChangesHistory` or
  `ProfileEvents`.
- **No copying from other refs.** Do not `git show <other-sha>`,
  `git log <other-branch>`, or read other branches/tags to figure out
  "what should be there". The only refs that matter are
  `{source_pr_merge_sha}`, `{port_branch}`, `{base_branch}`, `MERGE_HEAD`,
  and the merge-base `<MB>` from Step 1.
- **No `git add -A`.** Stage only the conflicted files (and any file
  you had to touch to make them compile after resolving the conflict).
- **No fixing unrelated lints / refactors / typos** noticed along the
  way.

### Step 4 — Verify scope before committing

After editing the conflicted files but BEFORE running `git commit
--no-edit`:

```bash
git diff -- <file>
```

Read each `+` line and classify it into one of the three allowed
buckets:

- **Bucket (a) — in the source PR diff?** Confirm via `git show -m
  --first-parent {source_pr_merge_sha}`. Keep it.
- **Bucket (b) — in `git diff <MB> MERGE_HEAD`?** Confirm via that
  command. Keep it.
- **Bucket (c) — minimal mechanical adaptation?** Try to fill in this
  sentence out loud:

  > "I had to write this line because commit `<sha>` (or symbol
  > `<name>`) on `{base_branch}` `<renamed | moved | changed the
  > signature of | split | removed>` `<exact thing>`, which collides
  > with the source PR's use of it. The change I made is the minimal
  > translation: just `<token swap | extra arg | new include path |
  > …>`."

  You should be able to point to the specific commit / symbol in the
  `git log <MB>..MERGE_HEAD` output from Step 1. Vague answers ("the
  API looks different now", "to match surrounding style", "it seems
  consistent with X") do **not** count.
- **None of (a), (b), (c) → remove the line and redo the resolution.**
  If removing it breaks the build in Step 6, that's evidence you
  misidentified bucket (c) — re-examine, find the named base-branch
  change, and try again.

If after this check you genuinely cannot decide between two reasonable
resolutions of a hunk, or a bucket-c adaptation would require more than
a token-level change, stop, print a single line `UNRESOLVED` and exit.
A clean abort is much better than an over-eager guess.

### Step 5 — Stage and conclude the merge

```bash
git add <file> <file> ...
git commit --no-edit
```

Git has already prepared a merge commit message; you only need to seal
it.

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

If the build fails:
- The Bash tool result may be truncated. The full log is at
  `{build_log}`. Use the **Read** tool on it (with `offset` / `limit`)
  or the **Grep** tool (e.g. `pattern: "error:"`, `pattern: "FAILED"`)
  to find the actual failure.
- Fix the offending code — the same scope rule still applies. Your fix
  must remain inside the source PR's diff plus the base branch's recent
  diff plus minimal mechanical adaptations. Do not "fix" the build by
  pulling in code from other PRs.
- Stage and amend the merge commit:
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
  any other command that mutates the remote. RelEasy will push the
  branch after you finish. (Read-only `gh` commands like
  `gh pr diff {source_pr_url}` are fine and encouraged.)
- Never force-push to `{base_branch}` or any protected branch.
- Never amend or rewrite commits that already exist on
  `origin/{base_branch}` or on `origin/{port_branch}` (those are the
  previous tip of the rebase PR — leave them alone, only the new merge
  commit is yours to seal/amend).
- Do not run `git reset --hard` against any remote ref. In particular,
  do not run `git merge --abort`, `git reset --hard HEAD~`, or anything
  that would discard the merge in progress — if you can't resolve, just
  exit with `UNRESOLVED` and let RelEasy clean up.
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
  line falls into one of the three allowed buckets — (a) source PR
  diff, (b) `git diff <MB> MERGE_HEAD`, or (c) a named, minimal,
  token-level mechanical adaptation between (a) and (b) — stop, print
  a single line `UNRESOLVED` and exit. Do not guess.
- On success, your final line of output must be `DONE`.

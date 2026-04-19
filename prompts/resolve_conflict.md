# Claude skill: resolve a RelEasy port cherry-pick conflict

You are an autonomous agent resolving a git cherry-pick conflict in `{repo_slug}`.

The repository at `{cwd}` is already prepared for you:

- Current branch: `{port_branch}` (already checked out).
- A cherry-pick is **in progress** and has hit conflict markers.
- The target base branch is `{base_branch}` (exists on origin).
- The port is of source PR [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".

> **NOTE:** This is one step of a larger pipeline. Your job ends after the
> conflict is resolved, the build succeeds, and the cherry-pick has been
> committed locally. RelEasy itself owns pushing the branch, opening the
> pull request, and applying labels. **Do not push, do not open a PR, do
> not run `gh pr ...`.**

## Conflicted files

{conflict_files}

## PR body (context, may be empty)

{source_pr_body}

## Task — execute these steps in order, without asking for confirmation

1. **Resolve the conflicts** in each of the listed files. Preserve the intent of
   the source PR. Do not introduce unrelated changes. Read the surrounding
   code to understand the merge context before editing.
2. **Stage** only the conflicted files or files modified when resolving conflicts: `git add <file> <file> ...`.
   Do not `git add -A` — avoid accidentally committing build artefacts.
3. **Continue the cherry-pick**: `git cherry-pick --continue --no-edit`.
4. **Build** the project to verify the resolution compiles.

   RelEasy has written a wrapper script at `{build_script}` that
   contains the exact build commands configured for this project
   (essentially: `{build_command}`). It internally tees full output
   to `{build_log}`.

   Run the build with **exactly this single Bash command** — no
   subshells, no `&&`, no `;`, no `bash -c '…'`:

   ```bash
   bash {build_script}
   ```

   Rules for this step:
   - Use the line above verbatim. Do not invent your own `cmake` /
     `ninja` invocations, do not chain extra commands with `&&` or
     `;`, do not wrap it in `(...)` or `bash -c '…'`. Claude's
     Bash tool will reject any of those.
   - Do not redirect output to other files. The script already tees
     into `{build_log}`.

   If the build fails:
   - The Bash tool result may be truncated. The full log is at
     `{build_log}`. Use the **Read** tool on it (with `offset` /
     `limit`) or the **Grep** tool (e.g. `pattern: "error:"`,
     `pattern: "FAILED"`) to find the actual failure.
   - Fix the offending code.
   - Stage and amend the commit: `git add -u && git commit --amend --no-edit`.
   - Rerun the EXACT same single command `bash {build_script}` (it
     overwrites the log, which is fine).
   - You may iterate at most **{max_iterations}** build attempts in total.
5. Verify the working tree is clean: `git status --porcelain` must produce
   no output. If it does, stage and amend (`git add -u && git commit --amend --no-edit`).

## Hard rules

- You are only allowed to touch branch `{port_branch}`. Never check out, push, or delete any other branch.
- **Never push.** Never run `git push`, `gh pr create`, `gh pr edit`, or any other command that mutates the remote. RelEasy will push and open the PR after you finish.
- Never force-push to `{base_branch}` or any protected branch.
- Never amend or rewrite commits that already exist on `origin/{base_branch}`.
- Do not run `git reset --hard` against any remote ref.
- Never write log files yourself; the only build log is `{build_log}`, and it is produced by the wrapper script — you only ever read it.
- Never invoke `cmake`, `ninja`, `make`, or `bash` directly with custom arguments. The only allowed build invocation is the single command `bash {build_script}`.
- Never use compound Bash commands (`&&`, `||`, `;`, `(...)`, `{ ... }`, `bash -c '…'`). Claude's Bash tool refuses them. Run one simple command per Bash call.
- If after **{max_iterations}** build attempts the build still fails, stop, print a single line `BUILD FAILED` and exit.
- If the conflicts cannot be resolved with confidence, stop, print a single line `UNRESOLVED` and exit.
- On success, your final line of output must be `DONE`.

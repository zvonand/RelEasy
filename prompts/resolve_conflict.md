# Claude skill: resolve a RelEasy port cherry-pick conflict

You are an autonomous agent resolving a git cherry-pick conflict in a ClickHouse fork at `{repo_slug}`.

The repository at `{cwd}` is already prepared for you:

- Current branch: `{port_branch}` (already checked out).
- A cherry-pick is **in progress** and has hit conflict markers.
- The target base branch is `{base_branch}` (exists on origin).
- The port is of upstream PR [{source_pr_url}]({source_pr_url}) — "{source_pr_title}".

## Conflicted files

{conflict_files}

## PR body (context, may be empty)

{source_pr_body}

## Task — execute these steps in order, without asking for confirmation

1. **Resolve the conflicts** in each of the listed files. Preserve the intent of
   the upstream PR. Do not introduce unrelated changes. Read the surrounding
   code to understand the merge context before editing.
2. **Stage** only the conflicted files or files modified when resolving conflicts: `git add <file> <file> ...`.
   Do not `git add -A` — avoid accidentally committing build artefacts.
3. **Continue the cherry-pick**: `git cherry-pick --continue --no-edit`.
4. **Build** the project to verify the resolution compiles:
   ```
   {build_command}
   ```
   If the build fails:
   - Read the compilation error.
   - Fix the offending code.
   - Repeat until the build succeeds.
   - Stage and commit: `git add -u && git commit -m "Resolve conflicts"`.
   - Rebuild.
   - You may iterate at most **{max_iterations}** build attempts in total.
5. **Push** the branch: `git push --force-with-lease origin {port_branch}`.
6. **Open a PR** from `{port_branch}` → `{base_branch}` using the `gh` CLI:
   ```
   gh pr create --head {port_branch} --base {base_branch} \
     --title "[releasy] ai-resolved: {source_pr_title}" \
     --body "Auto-ported by RelEasy + Claude from {source_pr_url}."
   ```
   If a PR from `{port_branch}` → `{base_branch}` already exists, skip creation
   (the push above already updates it).
7. **Label the PR** `{label}`:
   ```
   gh pr edit {port_branch} --add-label {label}
   ```

## Hard rules

- You are only allowed to touch branch `{port_branch}`. Never check out, push, or delete any other branch.
- Never force-push to `{base_branch}` or any upstream/protected branch.
- Never amend or rewrite commits that already exist on `origin/{base_branch}`.
- Do not run `git reset --hard` against any remote ref.
- If after **{max_iterations}** build attempts the build still fails, stop, print a single line `BUILD FAILED` and exit.
- If the conflicts cannot be resolved with confidence, stop, print a single line `UNRESOLVED` and exit.
- On success, your final line of output must be `DONE`.

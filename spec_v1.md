# RelEasy — Specification (v1)

## 1. Overview

A standalone CLI tool that manages rebasing Altinity's ClickHouse fork branches onto a specified upstream commit. Instead of rebasing long-lived branches in place, it creates **versioned branches** from clean upstream commits and cherry-picks changes on top.

The tool manages one CI base branch and any number of feature branches, runs them through a two-stage pipeline, and reports status to a `STATUS.md` file and optionally a GitHub Project board.

The tool lives in its own repository alongside its configuration and state files.

---

## 2. Repository Layout

```
releasy/
├── config.yaml        ← fork description, upstream URL, branch list
├── state.yaml         ← persisted pipeline state (auto-updated by tool)
├── STATUS.md          ← human-readable branch status table (auto-updated by tool)
└── src/releasy/       ← tool source code
```

---

## 3. Branch Naming Convention

All managed branches follow the pattern `<type>/<project>/<name>/<sha8>`:

| Type | Pattern | Example |
|------|---------|---------|
| CI | `ci/<project>/<sha8>` | `ci/antalya/abc12345` |
| Feature | `feature/<project>/<id>/<sha8>` | `feature/antalya/exportmergetree/abc12345` |

- `<project>` is derived from `ci.branch_prefix` in config (e.g. `ci/antalya` → project = `antalya`)
- `<sha8>` is the first 8 characters of the upstream commit the branch is based on
- Old versioned branches stay on the remote as history — nothing is force-pushed or destroyed

---

## 4. Configuration (`config.yaml`)

```yaml
upstream:
  remote: https://github.com/ClickHouse/ClickHouse.git
  remote_name: upstream

fork:
  remote: https://github.com/Altinity/ClickHouse.git
  remote_name: origin

ci:
  branch_prefix: ci/antalya         # versioned branches: ci/antalya/<sha8>
  source_branch: antalya-ci          # initial source branch for bootstrap

features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    source_branch: feature/antalya-s3-disk
    enabled: true

  - id: keeper-cfg
    description: "Keeper config extensions"
    source_branch: feature/antalya-keeper-cfg
    enabled: true

  - id: observability
    description: "Observability hooks"
    source_branch: feature/antalya-observability
    enabled: false                  # excluded from pipeline and releases

  - id: disk-cache
    description: "Shared disk cache layer"
    source_branch: feature/antalya-disk-cache
    depends_on: [s3-disk]           # applied after s3-disk (dependency ordering TBD)
    enabled: true

# Optional: sync status to a GitHub Project board
# notifications:
#   github_project: https://github.com/orgs/Altinity/projects/1
```

**Notes:**
- `enabled: false` excludes a feature from both the maintenance pipeline and release construction.
- `source_branch` is only used on the first run (bootstrap). After that, state tracks the current versioned branch.
- Feature order in the list determines apply order during release construction.
- `depends_on` declares that a feature requires another feature's commits to be present. Dependency-aware ordering is planned for a future version; for now, list dependent features after their dependencies in the config.

---

## 5. Authentication

- **Git operations** (clone, fetch, push): SSH key. The tool uses the key available via the SSH agent, or the path set in `RELEASY_SSH_KEY_PATH`.
- **GitHub API operations** (PR creation, project board sync): Personal Access Token set via `RELEASY_GITHUB_TOKEN`. Requires `repo` and `project` scopes.

---

## 6. Pipeline: Continuous Maintenance

### Invocation

```
releasy run --onto <upstream-commit-sha>
```

The operator explicitly specifies the upstream commit SHA. The tool does not auto-discover upstream HEAD.

---

### Stage 1 — Create CI Branch

1. Fetch upstream and fork remotes.
2. Determine the source of CI commits:
   - **First run:** use `ci.source_branch` from config; find divergence point via `git merge-base` against the `--onto` commit.
   - **Subsequent runs:** use the previous versioned CI branch from state; the base commit is known exactly.
3. Create a clean branch `ci/<project>/<sha8>` from `<upstream-commit-sha>`.
4. Cherry-pick CI commits (divergence..source-tip) onto the new branch.
5. **On success:**
   - Push the new CI branch to fork remote.
   - Update `state.yaml`: set `ci_branch.status = ok`, `ci_branch.branch_name`, `ci_branch.base_commit`.
   - Update `STATUS.md`.
   - Proceed to Stage 2.
6. **On conflict:**
   - Leave the branch as-is (cherry-pick is aborted).
   - Update `state.yaml`: set `ci_branch.status = conflict`, record `conflict_files`.
   - Update `STATUS.md`.
   - **Halt. Do not proceed to Stage 2.**

---

### Stage 2 — Create Feature Branches (Sequential)

For each enabled feature, in config order:

1. Determine the source of feature commits:
   - **First run:** use `source_branch` from config; find divergence via `merge-base` against CI source.
   - **Subsequent runs:** use the previous versioned feature branch from state; divergence is the old CI branch tip.
2. Create a clean branch `feature/<project>/<id>/<sha8>` from the new CI branch.
3. Cherry-pick feature commits onto the new branch.
4. **On success:**
   - Push the new feature branch to fork remote.
   - Update `state.yaml` and `STATUS.md`.
   - Proceed to the next feature.
5. **On conflict:**
   - Leave the branch as-is (cherry-pick is aborted).
   - Update `state.yaml` and `STATUS.md`.
   - **Proceed to the next feature** (conflict on one branch does not block others).

Pipeline run is complete when all enabled features have been attempted.

---

## 7. Conflict Resolution Flow

1. Operator sees `STATUS.md`, `releasy status`, or the GitHub Project board.
2. Operator resolves the conflict locally:
   - Check out the versioned branch (e.g. `feature/antalya/s3-disk/abc12345`).
   - Cherry-pick the remaining commits, resolve conflicts, force-push.
3. Operator signals resolution:
   ```
   releasy continue --branch <branch-name-or-feature-id>
   ```
4. Tool marks the branch `resolved` in `state.yaml`, updates `STATUS.md`.

**Skip:** if a branch is not going to be resolved in this run:
```
releasy skip --branch <branch-name-or-feature-id>
```
Marks the branch `skipped` in state. Skipped branches are excluded from release construction unless `--include-skipped` is passed.

---

## 8. State (`state.yaml`)

Auto-updated by the tool after every operation. Committed and pushed to the tool repo after each update.

```yaml
last_run:
  started_at: 2026-03-21T10:00:00Z
  onto: abc1234567890
  ci_branch:
    status: ok                  # pending | ok | conflict | resolved | skipped
    branch_name: ci/antalya/abc12345
    base_commit: abc1234567890
  features:
    s3-disk:
      status: ok
      branch_name: feature/antalya/s3-disk/abc12345
      base_commit: abc1234567890
    keeper-cfg:
      status: conflict
      branch_name: feature/antalya/keeper-cfg/abc12345
      base_commit: abc1234567890
      conflict_files:
        - src/Storages/StorageS3.cpp
    observability:
      status: disabled
```

---

## 9. Status Table (`STATUS.md`)

Updated after every state change. Committed and pushed to the tool repo automatically.

```markdown
## RelEasy Branch Status

Last run: 2026-03-21 · Upstream commit: `abc1234567890`

| Branch                                    | Status      | Based On       | Conflict Files              |
|-------------------------------------------|-------------|----------------|-----------------------------|
| ci/antalya/abc12345                       | ✅ ok       | `abc12345678`  |                             |
| feature/antalya/s3-disk/abc12345          | ✅ ok       | `abc12345678`  |                             |
| feature/antalya/keeper-cfg/abc12345       | 🔴 conflict | `abc12345678`  | `src/Storages/StorageS3.cpp` |
| feature/antalya-observability             | ⏸ disabled  |                |                             |
```

Status values: `✅ ok` · `🔴 conflict` · `🔵 resolved` · `⏭ skipped` · `⏸ disabled` · `⏳ pending`

---

## 10. Release Branch Construction (PR-per-Feature)

On-demand operation, separate from the maintenance pipeline. Creates a base release branch and opens individual PRs for each feature.

```
releasy release --upstream-tag <tag> --name <branch-name>
```

Example:
```
releasy release --upstream-tag v26.1.3 --name antalya-26.1
```

**Steps:**
1. Warn if any enabled features have status `conflict` or `skipped` (does not abort by default; add `--strict` to abort on warnings).
2. Create branch `<name>` from the specified upstream tag.
3. Cherry-pick CI branch commits onto the base branch (all CI commits as-is, not squashed).
4. Push the base branch to fork remote.
5. For each enabled (and non-skipped) feature branch in config order:
   - Create branch `<name>/feat/<feature-id>` from the base branch.
   - Squash all feature commits into a single commit.
   - Cherry-pick that commit onto the feature PR branch.
   - Commit message format: `[releasy] <feature-id>: <feature-description>`.
   - Push the branch and open a GitHub PR targeting `<name>`.
   - If `depends_on` is set, the PR body notes which PRs must be merged first.
6. **On conflict for a feature:** skip that feature (other features still get PRs).
7. Print summary with all PR URLs.

**PR naming:**
- Branch: `<release-name>/feat/<feature-id>` (e.g. `antalya-26.1/feat/s3-disk`)
- Title: `[releasy] <feature-id>: <feature-description>`
- Each PR contains exactly one squashed commit for clean review.

**Merge order:** merge PRs in config order, respecting `depends_on` declarations.

**Flags:**
- `--strict` — abort if any enabled feature is not `ok`.
- `--include-skipped` — include `skipped` features in release construction.
- If `RELEASY_GITHUB_TOKEN` is not set, branches are pushed but PRs are not created (manual PR creation required).

---

## 11. GitHub Project Integration

Instead of opening GitHub issues (which pollutes the repository), RelEasy syncs branch status to a GitHub Projects v2 board.

**Setup:**
1. Create a GitHub Project (org or user level).
2. Add a single-select **Status** field with options: `Ok`, `Conflict`, `Resolved`, `Skipped`, `Disabled`, `Pending`.
3. Set `notifications.github_project` URL in config.
4. Set `RELEASY_GITHUB_TOKEN` with `project` scope.

RelEasy creates one draft-issue card per branch and keeps the Status field in sync after each pipeline operation.

This is optional — `STATUS.md` and `releasy status` work without any GitHub integration.

---

## 12. CLI Reference

```
# Maintenance pipeline
releasy run --onto <sha>                # create versioned branches from upstream commit
releasy continue --branch <name>        # mark a conflict-resolved branch
releasy skip --branch <name>            # skip a conflicted branch for this run
releasy abort                           # abort current run, leave all branches as-is
releasy status                          # print current pipeline state to stdout

# Release
releasy release \
  --upstream-tag <tag> \
  --name <branch-name> \
  [--strict] \
  [--include-skipped]

# Config management
releasy feature add \
  --id <id> \
  --source-branch <branch> \
  --description <desc>
releasy feature enable  --id <id>
releasy feature disable --id <id>
releasy feature remove  --id <id>
releasy feature list
```

---

## 13. Non-Goals (v1)

- No automatic upstream commit discovery (`--onto` is always explicit).
- No AI-assisted conflict resolution.
- No support for multiple forks.
- No automatic dependency-aware ordering (`depends_on` is declared but ordering is manual in v1).
- No build/test validation after rebase.
- No GitHub issues (status tracked in `STATUS.md` and optionally a GitHub Project board).

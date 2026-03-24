# Antalya Feature Management Tool — Specification

## 1. Overview

A standalone CLI tool that automates rebasing Altinity's ClickHouse fork branches onto a specified upstream commit. It manages one CI base branch and any number of feature branches, runs them through a two-stage rebase pipeline, and reports status to a `STATUS.md` file and GitHub issues in its own repository.

The tool lives in its own repository alongside its configuration and state files.

---

## 2. Repository Layout

```
antalya/
├── config.yaml        ← fork description, upstream URL, branch list
├── state.yaml         ← persisted pipeline state (auto-updated by tool)
├── STATUS.md          ← human-readable branch status table (auto-updated by tool)
└── src/               ← tool source code
```

---

## 3. Configuration (`config.yaml`)

```yaml
upstream:
  remote: https://github.com/ClickHouse/ClickHouse.git
  remote_name: upstream

fork:
  remote: https://github.com/Altinity/ClickHouse.git
  remote_name: origin

ci_branch: antalya-ci

features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    branch: feature/antalya-s3-disk
    enabled: true

  - id: keeper-cfg
    description: "Keeper config extensions"
    branch: feature/antalya-keeper-cfg
    enabled: true

  - id: observability
    description: "Observability hooks"
    branch: feature/antalya-observability
    enabled: false                  # excluded from pipeline and releases

notifications:
  github_issues: true               # open one issue per conflicted branch; auto-close on resolution
```

**Notes:**
- `enabled: false` excludes a feature from both the maintenance pipeline and release construction.
- Feature order in the list determines apply order during release construction.
- Features that depend on each other must be kept in a single branch. The tool does not model intra-feature dependencies.

---

## 4. Authentication

- **Git operations** (clone, fetch, push): SSH key. The tool uses the key available via the SSH agent, or the path set in `ANTALYA_SSH_KEY_PATH`.
- **GitHub API operations** (update `STATUS.md`, open/close issues): Personal Access Token set via `ANTALYA_GITHUB_TOKEN`. Requires `repo` scope.

---

## 5. Pipeline: Continuous Maintenance

### Invocation

```
antalya run --onto <upstream-commit-sha>
```

The operator explicitly specifies the upstream commit SHA to rebase onto. The tool does not auto-discover upstream HEAD.

---

### Stage 1 — Rebase CI Branch

1. Fetch upstream remote.
2. Fetch fork remote.
3. Check out `antalya-ci` locally.
4. Run `git rebase <upstream-commit-sha>`.
5. **On success:**
   - Force-push `antalya-ci` to fork remote.
   - Update `state.yaml`: set `ci_branch.status = ok`, `ci_branch.rebased_onto = <sha>`.
   - Update `STATUS.md`.
   - Proceed to Stage 2.
6. **On conflict:**
   - Abort the rebase (`git rebase --abort`), leaving the branch untouched.
   - Update `state.yaml`: set `ci_branch.status = conflict`, record `conflict_files`.
   - Update `STATUS.md`.
   - Open a GitHub issue: `[conflict] antalya-ci onto <sha>` listing conflicting files.
   - **Halt. Do not proceed to Stage 2.**

Stage 2 does not start until Stage 1 completes cleanly. This ensures all feature branches rebase onto the updated CI layer.

---

### Stage 2 — Rebase Feature Branches (Sequential)

For each enabled feature branch, in config order:

1. Check out the feature branch locally.
2. Run `git rebase antalya-ci`.
3. **On success:**
   - Force-push the branch to fork remote.
   - Update `state.yaml`: set feature `status = ok`, `rebased_onto = <sha>`.
   - Update `STATUS.md`.
   - Proceed to the next feature.
4. **On conflict:**
   - Abort the rebase (`git rebase --abort`), leaving the branch untouched.
   - Update `state.yaml`: set feature `status = conflict`, record `conflict_files`.
   - Update `STATUS.md`.
   - Open a GitHub issue: `[conflict] <branch> onto <sha>` listing conflicting files.
   - **Proceed to the next feature** (conflict on one branch does not block others).

Pipeline run is complete when all enabled features have been attempted.

---

## 6. Conflict Resolution Flow

1. Operator sees the GitHub issue and/or `STATUS.md`.
2. Operator resolves the conflict locally:
   - Check out the branch.
   - Run `git rebase antalya-ci` (or `upstream/<sha>` for the CI branch), resolve conflicts, complete rebase.
   - Force-push the branch.
3. Operator signals resolution:
   ```
   antalya continue --branch <branch-name>
   ```
4. Tool verifies the push (checks that the branch tip has the expected rebase onto target), marks the branch `resolved` in `state.yaml`, updates `STATUS.md`, closes the GitHub issue.

**Skip:** if a branch is not going to be resolved in this run:
```
antalya skip --branch <branch-name>
```
Marks the branch `skipped` in state. Skipped branches are excluded from release construction unless `--include-skipped` is passed.

---

## 7. State (`state.yaml`)

Auto-updated by the tool after every operation. Committed and pushed to the tool repo after each update.

```yaml
last_run:
  started_at: 2026-03-21T10:00:00Z
  onto: abc1234
  ci_branch:
    status: ok                  # pending | ok | conflict | resolved | skipped
    rebased_onto: abc1234
  features:
    s3-disk:
      status: ok
      rebased_onto: abc1234
    keeper-cfg:
      status: conflict
      conflict_files:
        - src/Storages/StorageS3.cpp
    observability:
      status: disabled
```

---

## 8. Status Table (`STATUS.md`)

Updated after every state change. Committed and pushed to the tool repo automatically.

```markdown
## Antalya Branch Status

Last run: 2026-03-21 · Upstream commit: `abc1234`

| Branch                         | Status      | Last Rebased | Conflict Files              |
|--------------------------------|-------------|--------------|-----------------------------|
| antalya-ci                     | ✅ ok       | 2026-03-21   |                             |
| feature/antalya-s3-disk        | ✅ ok       | 2026-03-21   |                             |
| feature/antalya-keeper-cfg     | 🔴 conflict | 2026-03-20   | src/Storages/StorageS3.cpp  |
| feature/antalya-observability  | ⏸ disabled  |              |                             |
```

Status values: `✅ ok` · `🔴 conflict` · `🔵 resolved` · `⏭ skipped` · `⏸ disabled` · `⏳ pending`

---

## 9. Release Branch Construction

On-demand operation, separate from the maintenance pipeline.

```
antalya release --upstream-tag <tag> --name <branch-name>
```

Example:
```
antalya release --upstream-tag v26.1.3 --name antalya-26.1
```

**Steps:**
1. Warn if any enabled features have status `conflict` or `skipped` (does not abort by default; add `--strict` to abort on warnings).
2. Create branch `<name>` from the specified upstream tag.
3. Squash-rebase `antalya-ci` commits onto the branch (all CI commits as-is, not squashed).
4. For each enabled (and non-skipped) feature branch in config order:
   - Squash all commits on the feature branch into a single commit.
   - Rebase that commit onto the release branch.
   - Commit message format: `[antalya] <feature-id>: <feature-description>`.
5. **On conflict at any step:** halt, report the conflicting branch and files, prompt for `continue` / `skip` / `abort`.
6. **On success:** push the release branch to fork remote.

**Flags:**
- `--strict` — abort if any enabled feature is not `ok`.
- `--include-skipped` — include `skipped` features in release construction.

---

## 10. CLI Reference

```
# Maintenance pipeline
antalya run --onto <sha>           # run full pipeline onto specified upstream commit
antalya continue --branch <name>   # mark a conflict-resolved branch and resume
antalya skip --branch <name>       # skip a conflicted branch for this run
antalya abort                      # abort current run, leave all branches as-is
antalya status                     # print current pipeline state to stdout

# Release
antalya release \
  --upstream-tag <tag> \
  --name <branch-name> \
  [--strict] \
  [--include-skipped]

# Config management
antalya feature add \
  --id <id> \
  --branch <branch> \
  --description <desc>
antalya feature enable  --id <id>
antalya feature disable --id <id>
antalya feature remove  --id <id>
antalya feature list
```

---

## 11. Non-Goals (v1)

- No automatic upstream commit discovery (`--onto` is always explicit).
- No AI-assisted conflict resolution.
- No support for multiple forks.
- No intra-feature dependencies (dependent features share one branch).
- No build/test validation after rebase.
- No GitHub Project board integration (STATUS.md is sufficient for v1).

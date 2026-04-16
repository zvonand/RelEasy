# RelEasy — Specification (v1)

## 1. Overview

A standalone CLI tool that manages rebasing branches onto a specified commit/tag. Instead of rebasing long-lived branches in place, it creates a **base branch** from the target ref, rebases CI and features on top via a **phased pipeline**, and optionally opens PRs. Works with a fork against an upstream, or within a single repo for forward-porting/back-porting.

The pipeline is **resumable**: state is persisted between runs, and each `releasy run` picks up from where the previous run stopped.

---

## 2. Repository Layout

```
releasy/
├── config.yaml        ← origin/upstream URLs, features, PR sources (gitignored)
├── config.yaml.example ← documented template
├── state.yaml         ← persisted pipeline state (auto-managed)
├── STATUS.md          ← human-readable branch status table (auto-managed)
└── src/releasy/       ← tool source code
```

---

## 3. Branch Naming Convention

Branch names are derived from the upstream ref:
- Tags like `v26.3.4.234-lts` → version suffix `26.3`
- Raw SHAs → first 8 characters

| Type | Pattern | Example |
|------|---------|---------|
| Base | `<project>-<version>` | `antalya-26.3` |
| CI | `ci/<project>-<version>` | `ci/antalya-26.3` |
| Feature | `feature/<project>-<version>/<id>` | `feature/antalya-26.3/s3-disk` |
| PR Feature | `feature/<project>-<version>/pr-<N>` | `feature/antalya-26.3/pr-42` |

- `<project>` is derived from `ci.branch_prefix` in config (e.g. `ci/antalya` → project = `antalya`)
- Old branches stay on the remote as history — nothing is force-pushed or destroyed

---

## 4. Configuration (`config.yaml`)

```yaml
push: false              # push branches and open PRs (default: false)
work_dir: /path/to/repo  # path to existing clone or where to clone

# Origin (required) — the repo you work against
origin:
  remote: https://github.com/Altinity/ClickHouse.git
  remote_name: origin

# Upstream (optional) — omit entirely if porting within origin
upstream:
  remote: https://github.com/ClickHouse/ClickHouse.git
  remote_name: upstream

ci:
  branch_prefix: ci/antalya
  source_branch: antalya-ci    # omit or leave empty to skip CI rebase
  if_exists: skip              # skip (default) or redo

features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    source_branch: feature/antalya-s3-disk
    enabled: true
  - id: keeper-cfg
    description: "Keeper config extensions"
    source_branch: feature/antalya-keeper-cfg
    depends_on: [s3-disk]
    enabled: true

# PR discovery and filtering
# Set arithmetic: union(by_labels) − exclude_labels + include_prs − exclude_prs
pr_sources:
  by_labels:
    - labels: ["forward-port", "v26.3"]
      description: "Forward-ported changes"
      merged_only: true
      auto_pr: true
      if_exists: skip

  exclude_labels: ["do-not-port"]

  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/123

  exclude_prs:
    - https://github.com/Altinity/ClickHouse/pull/789

notifications:
  github_project: https://github.com/orgs/Altinity/projects/1
```

**Notes:**
- `push: false` (default) keeps everything local — no branches pushed, no PRs created, no project board sync.
- `work_dir` can point to an existing git clone. If the directory has a `.git`, it's used directly (no clone).
- `origin` is required — the repo where branches are pushed and PRs are created.
- `upstream` is optional — omit it when forward-porting or back-porting within your own repo.
- `ci.source_branch` empty or omitted → CI rebase phase is skipped entirely.
- `ci.if_exists` / `by_labels[].if_exists`: `skip` (default) leaves existing branches alone; `redo` recreates from scratch.
- `pr_sources.by_labels[].labels`: AND logic — a PR must have ALL listed labels. Multiple `by_labels` entries are unioned.
- `pr_sources.exclude_labels`: any PR carrying at least one of these labels is dropped (unless it appears in `include_prs`).
- `pr_sources.include_prs`: always include these PRs by URL, regardless of labels.
- `pr_sources.exclude_prs`: always exclude these PRs by URL — final override.
- PRs are processed in merge order (earliest merged first).
- `enabled: false` excludes a feature from the pipeline entirely.
- `depends_on` declares inter-feature dependencies (manual ordering in v1).

---

## 5. Authentication

- **Git operations** (clone, fetch, push): SSH key via agent or `RELEASY_SSH_KEY_PATH`.
- **GitHub API** (PR discovery, PR creation, project board): `RELEASY_GITHUB_TOKEN` with `repo` and `project` scopes.

---

## 6. Pipeline: Phased Rebase

### Invocation

```
releasy run --onto <upstream-tag-or-sha>
```

The pipeline runs in phases. State is saved after each phase so it can be resumed.

---

### Phase 1a — Create Base Branch

1. Fetch upstream (if configured) and origin remotes.
2. Parse the `--onto` ref to derive the version suffix (e.g. `v26.3.4.234-lts` → `26.3`).
3. Create base branch `<project>-<version>` from the target ref.
4. Push to origin (if `push: true`).
5. Save state: `phase = base_created`.

---

### Phase 1b — Rebase CI onto Base

**Skipped** if `ci.source_branch` is empty or omitted.

1. Find CI commits: `merge-base` between `source_branch` and upstream ref.
2. Create CI branch `ci/<project>-<version>` from the base branch.
3. `git rebase --onto <base> <divergence> <source_tip>` — replays CI commits, automatically dropping commits already in upstream.
4. Push and open PR: CI → base (if `push: true`).
5. Save state: `phase = ci_rebased`.
6. **Stop.** Print instructions to merge the CI PR before proceeding.

**On conflict:** the rebase is left in progress. User resolves, runs `git rebase --continue`, then `releasy continue --branch <ci-branch>`.

---

### Phase 2 — Rebase Features onto Base

Runs when `phase = ci_rebased` (or `ci_merged`).

#### PR-based features

PRs are collected and filtered using set arithmetic:

1. **Collect:** For each `by_labels` entry, search GitHub for PRs matching all labels (Issues API, AND logic). Union the results.
2. **Exclude by label:** Remove any PR carrying at least one `exclude_labels` label (unless it's in `include_prs`).
3. **Include individual PRs:** Fetch any `include_prs` URLs not already in the set.
4. **Exclude individual PRs:** Remove any `exclude_prs` URLs — final override.

For each PR in the filtered set:

1. Create feature branch `feature/<project>-<version>/pr-<N>` from base.
2. Cherry-pick the merge commit (`-m 1`).
3. On conflict: commit conflict markers as WIP, push anyway.
4. Open PR: feature → base (if `push: true` + `auto_pr: true`).
5. Conflict PRs include a warning banner in the PR body.

#### Static feature branches

For each enabled feature with a `source_branch`:

1. Find feature commits via `merge-base` against CI source.
2. Create feature branch `feature/<project>-<version>/<id>` from base.
3. `git rebase --onto <base> <divergence> <source_tip>`.
4. Push and open PR (if `push: true` + `auto_pr: true`).
5. On conflict: rebase left in progress for manual resolution.

After all features: `phase = features_done`.

---

## 7. Conflict Resolution

```
releasy continue --branch <branch-name-or-feature-id>
```

The tool verifies no git operation is still in progress before marking resolved.

```
releasy skip --branch <branch-name-or-feature-id>
```

Marks a branch as skipped (excluded from releases unless `--include-skipped`).

---

## 8. Safety: Upstream PR Protection

When an upstream is configured, `create_pull_request` has a **hard safeguard**: before creating any PR, it compares the origin repo slug against the upstream remote. If they match, it raises a `ValueError` and refuses. When no upstream is configured, this check is skipped (origin is the only repo).

---

## 9. State (`state.yaml`)

Auto-updated after every operation.

```yaml
last_run:
  started_at: 2026-03-21T10:00:00Z
  onto: v26.3.4.234-lts
  phase: ci_rebased          # init | base_created | ci_rebased | ci_merged | features_done
  base_branch: antalya-26.3
  ci_branch:
    status: ok
    branch_name: ci/antalya-26.3
    base_commit: v26.3.4.234-lts
    pr_url: https://github.com/Altinity/ClickHouse/pull/100
  features:
    pr-42:
      status: ok
      branch_name: feature/antalya-26.3/pr-42
      base_commit: v26.3.4.234-lts
      pr_url: https://github.com/Altinity/ClickHouse/pull/42
      pr_number: 42
      pr_title: "Fix S3 disk timeout"
      rebase_pr_url: https://github.com/Altinity/ClickHouse/pull/101
    s3-disk:
      status: conflict
      branch_name: feature/antalya-26.3/s3-disk
      base_commit: v26.3.4.234-lts
      conflict_files:
        - src/Storages/StorageS3.cpp
```

---

## 10. Status Table (`STATUS.md`)

Updated after every state change.

```markdown
## RelEasy Branch Status

Last run: 2026-03-21 · Upstream commit: `v26.3.4.234-lts` · Phase: features_done

| Branch | Status | Based On | PR | Conflict Files |
|--------|--------|----------|----|----------------|
| ci/antalya-26.3 | ✅ ok | `v26.3.4.234-` | CI PR | |
| feature/antalya-26.3/pr-42 | ✅ ok | `v26.3.4.234-` | #42 | |
| feature/antalya-26.3/s3-disk | 🔴 conflict | `v26.3.4.234-` | | `src/Storages/StorageS3.cpp` |
```

---

## 11. CLI Reference

```
# Pipeline
releasy [--config <path>] run --onto <tag-or-sha> [--work-dir <path>]
releasy continue --branch <name>
releasy skip --branch <name>
releasy abort
releasy status

# Release
releasy release --upstream-tag <tag> --name <branch-name> [--strict] [--include-skipped]

# Feature management
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

---

## 12. GitHub Project Integration

Sync branch status to a GitHub Projects v2 board (optional). One-time manual setup, then RelEasy keeps it updated automatically.

### Manual Setup

1. **Create the project:** GitHub org → Projects → New project (Table layout recommended).
2. **Configure the Status field:** Edit the default "Status" single-select field. Set options to exactly: `Ok`, `Conflict`, `Resolved`, `Skipped`, `Disabled`, `Pending` (case-insensitive match).
3. **Get the URL:** e.g. `https://github.com/orgs/Altinity/projects/1` (org) or `https://github.com/users/<name>/projects/1` (personal).
4. **Token:** `RELEASY_GITHUB_TOKEN` needs `repo` + `project` scopes (classic PAT) or "Projects" read/write (fine-grained PAT).
5. **Config:**
   ```yaml
   push: true
   notifications:
     github_project: https://github.com/orgs/Altinity/projects/1
   ```

### Auto-setup

```
releasy setup-project
```

Creates the project (if needed) and adds the Status field with the correct options. Prints the URL to add to config.

### Automatic Behavior

When `push: true` and `notifications.github_project` is set, after each state change RelEasy:
- Creates a **view (tab)** per rebase, named after the base branch (e.g. `antalya-26.3`). One project holds all rebases; each gets its own tab.
- Creates one draft-issue card per branch (CI + each feature).
- Sets the Status field to the pipeline state (Ok, Conflict, etc.).
- Updates the card body with upstream commit and conflicted files.
- On re-runs, deletes old cards and recreates with updated status.

No cards or views need to be created manually. Project sync is skipped when `push: false`.

---

## 13. Non-Goals (v1)

- No automatic upstream commit discovery (`--onto` is always explicit).
- No AI-assisted conflict resolution.
- No support for multiple forks.
- No automatic dependency-aware ordering (`depends_on` declared but ordering is manual).
- No build/test validation after rebase.

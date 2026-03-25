# RelEasy

CLI tool for managing fork rebases, feature branches, and release construction.

RelEasy automates the process of maintaining a fork's branches against upstream. Instead of rebasing long-lived branches (which accumulates conflicts), it creates **versioned branches** from clean upstream commits and cherry-picks your changes on top.

## How It Works

```
upstream:  A --- B --- C --- D (upstream HEAD)
                       |
ci/antalya/C:          C --- x --- y (CI commits cherry-picked)
                                   |
feature/antalya/s3-disk/C:        C --- x --- y --- f1 --- f2
```

When you move to upstream commit `D`:

```
ci/antalya/D:                      D --- x' --- y'
                                               |
feature/antalya/s3-disk/D:                    D --- x' --- y' --- f1' --- f2'
```

Old branches stay on the remote as history — nothing is force-pushed or destroyed.

## Branch Naming

All branches follow the pattern `<type>/<project>/<name>/<sha8>`:

| Type | Example | Description |
|------|---------|-------------|
| CI | `ci/antalya/abc12345` | CI/infra changes on top of upstream `abc12345` |
| Feature | `feature/antalya/exportmergetree/abc12345` | Feature on top of CI branch based on `abc12345` |
| Feature | `feature/antalya/s3-disk/abc12345` | Another feature, same upstream base |
| PR Feature | `feature/antalya/pr-1234/abc12345` | PR #1234 cherry-picked onto CI branch |

The `<sha8>` is the first 8 characters of the upstream commit the branch is based on.

## Installation

```bash
pip install -e .
```

## Quick Start

1. Copy the example config and edit it for your fork:

```bash
cp config.yaml.example config.yaml
```

2. Set up authentication:

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."      # for PR discovery, PR creation & Project sync
export RELEASY_SSH_KEY_PATH="~/.ssh/id_rsa" # optional, defaults to SSH agent
```

3. Run the pipeline:

```bash
releasy run --onto <upstream-commit-sha>
```

This creates `ci/antalya/<sha8>` from the upstream commit, cherry-picks CI commits, then creates `feature/antalya/<id>/<sha8>` branches with feature commits.

## Configuration

```yaml
upstream:
  remote: https://github.com/ClickHouse/ClickHouse.git
  remote_name: upstream

fork:
  remote: https://github.com/Altinity/ClickHouse.git
  remote_name: origin

ci:
  branch_prefix: ci/antalya         # versioned branches: ci/antalya/<sha8>
  source_branch: antalya-ci          # initial source (first run only)

features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    source_branch: feature/antalya-s3-disk
    enabled: true

  - id: disk-cache
    description: "Shared disk cache layer"
    source_branch: feature/antalya-disk-cache
    depends_on: [s3-disk]           # ordering TBD — list after dependencies for now
    enabled: true

# PR-based features: discover PRs in the fork repo by label
pr_sources:
  - label: "forward-port"
    description: "Forward-ported changes"

# Optional: sync to a GitHub Project board
notifications:
  github_project: https://github.com/orgs/Altinity/projects/1
```

- The project name (`antalya`) is derived from `ci.branch_prefix` and used in all branch names
- `source_branch` is only used on the first run — after that, state tracks the current versioned branch
- Feature order determines apply order during release construction
- `enabled: false` excludes a feature from both the pipeline and release
- `depends_on` declares inter-feature dependencies; automatic ordering is planned — for now, list features in dependency order
- `pr_sources` discovers PRs by label in the fork repo and creates a feature branch per PR (requires `RELEASY_GITHUB_TOKEN`)

## CLI Reference

### Maintenance Pipeline

```bash
# Run full pipeline — create versioned branches from upstream commit
releasy run --onto <sha>

# Mark a conflict-resolved branch
releasy continue --branch <branch-or-feature-id>

# Skip a conflicted branch for this run
releasy skip --branch <branch-or-feature-id>

# Abort current run, leave all branches as-is
releasy abort

# Print current pipeline state
releasy status
```

### Release Construction

```bash
releasy release \
  --upstream-tag <tag> \
  --name <branch-name> \
  [--strict] \
  [--include-skipped]
```

Creates a release base branch and opens one PR per feature:

1. Creates `<branch-name>` from the upstream tag
2. Cherry-picks CI commits onto the base branch (infra, no PR needed)
3. Pushes the base branch
4. For each enabled feature:
   - Creates `<branch-name>/feat/<feature-id>` with a single squashed commit
   - Opens a PR targeting the base branch
   - PR title: `[releasy] <id>: <description>`

Each feature gets its own PR for independent review and CI. Merge PRs in config order (respecting `depends_on` if set).

Flags:
- `--strict` — abort if any enabled feature is not `ok`
- `--include-skipped` — include `skipped` features in release

If `RELEASY_GITHUB_TOKEN` is not set, branches are pushed but PRs are not created (manual PR creation required).

### Feature Management

```bash
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

## Pipeline Details

### Stage 1 — CI Branch

1. Create `ci/<project>/<sha8>` from the upstream commit
2. Find CI commits: on first run, uses `merge-base` between `source_branch` and upstream; on subsequent runs, uses the known base from state
3. Cherry-pick CI commits onto the clean branch
4. Push to fork remote

If cherry-pick conflicts, the pipeline halts and Stage 2 is skipped.

### Stage 2 — Feature Branches

For each enabled feature:

1. Create `feature/<project>/<id>/<sha8>` from the new CI branch
2. Cherry-pick feature commits from the previous versioned branch (or `source_branch` on first run)
3. Push to fork remote

Conflicts on one feature do not block others.

### Stage 3 — PR-Based Features (Bootstrap)

If `pr_sources` is configured, the tool searches the fork repo for PRs matching each label. Only **new** PRs are processed here — PRs from a previous run already have a versioned branch in state and are handled by Stage 2.

For each new PR:

1. Create `feature/<project>/pr-<number>/<sha8>` from the CI branch
2. Cherry-pick the PR's merge commit (`-m 1`) onto the feature branch
   - Merged PRs: use the actual merge commit SHA
   - Open PRs: fetch GitHub's auto-generated merge ref and cherry-pick it
3. Push to fork remote

After the first run, the PR feature behaves like a regular source-branch feature — Stage 2 cherry-picks from the previous versioned branch, preserving any conflict resolutions. The original PR title and description are stored in state and reused when creating release PRs.

## Conflict Resolution

1. Check `STATUS.md` or `releasy status` for conflicted branches
2. Resolve the conflict locally on the versioned branch, force-push
3. Signal resolution: `releasy continue --branch <name-or-id>`

## GitHub Project Integration

Optionally sync branch status to a GitHub Projects v2 board:

1. Create a project with a single-select **Status** field: `Ok`, `Conflict`, `Resolved`, `Skipped`, `Disabled`, `Pending`
2. Set `notifications.github_project` in config
3. Set `RELEASY_GITHUB_TOKEN` with `project` scope

## State Files

- `state.yaml` — tracks current versioned branch names, base commits, and status
- `STATUS.md` — human-readable status table

Both are committed and pushed to this repository after each state change.

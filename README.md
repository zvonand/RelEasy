# RelEasy

CLI tool for managing fork rebases, feature branches, and release construction.

RelEasy automates the process of rebasing a fork's branches onto upstream commits. It manages a CI base branch and any number of feature branches through a two-stage rebase pipeline, reporting status to a `STATUS.md` table and optionally syncing to a GitHub Project board.

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
export RELEASY_GITHUB_TOKEN="ghp_..."      # for GitHub Project sync (optional)
export RELEASY_SSH_KEY_PATH="~/.ssh/id_rsa" # optional, defaults to SSH agent
```

3. Run the rebase pipeline:

```bash
releasy run --onto <upstream-commit-sha>
```

## Configuration

RelEasy is configured via `config.yaml` in the repository root:

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

# Optional: sync to a GitHub Project board
notifications:
  github_project: https://github.com/orgs/Altinity/projects/1
```

- `enabled: false` excludes a feature from both the pipeline and release construction
- Feature order determines apply order during release construction
- Features that depend on each other should share a single branch

## GitHub Project Integration

RelEasy can optionally sync branch status to a GitHub Projects v2 board. To enable it:

1. Create a GitHub Project (org or user level)
2. Add a single-select **Status** field with options: `Ok`, `Conflict`, `Resolved`, `Skipped`, `Disabled`, `Pending`
3. Set the `notifications.github_project` URL in `config.yaml`
4. Set `RELEASY_GITHUB_TOKEN` with a PAT that has `project` scope

RelEasy creates one draft-issue card per branch and keeps the Status field in sync after each pipeline operation.

## CLI Reference

### Maintenance Pipeline

```bash
# Run full pipeline — rebase CI branch and all features onto upstream commit
releasy run --onto <sha>

# Mark a conflict-resolved branch
releasy continue --branch <branch-name>

# Skip a conflicted branch for this run
releasy skip --branch <branch-name>

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

Builds a release branch by:
1. Creating a branch from the upstream tag
2. Applying CI branch commits
3. Squash-applying each enabled feature (commit message: `[releasy] <id>: <description>`)

Flags:
- `--strict` — abort if any enabled feature is not `ok`
- `--include-skipped` — include `skipped` features in release

### Feature Management

```bash
releasy feature add --id <id> --branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

## Pipeline

### Stage 1 — Rebase CI Branch

Rebases the CI base branch onto the specified upstream commit. On conflict, the rebase is aborted and Stage 2 is skipped. The conflict is recorded in `STATUS.md` and the project board.

### Stage 2 — Rebase Feature Branches

Sequentially rebases each enabled feature branch onto the updated CI branch. Conflicts on one branch do not block others.

## Conflict Resolution

1. Check `STATUS.md` or the GitHub Project board for conflicted branches
2. Resolve the conflict locally, complete the rebase, force-push
3. Signal resolution: `releasy continue --branch <name>`
4. RelEasy updates state, `STATUS.md`, and the project board

## State Files

- `state.yaml` — machine-readable pipeline state (auto-updated)
- `STATUS.md` — human-readable status table (auto-updated)

Both are committed and pushed to this repository after each state change.

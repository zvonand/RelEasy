# RelEasy

CLI tool for managing fork rebases, feature branches, and release construction.

RelEasy automates maintaining a repo (typically a fork) against an optional upstream. Instead of rebasing long-lived branches (which accumulates conflicts), it creates a **base branch** from a tag/commit, rebases CI and features on top, and opens PRs — all in a phased, resumable workflow.

## TL;DR

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."

# With target_branch set in config.yaml:
releasy run

# Or, deriving the base branch from a tag:
releasy run --onto v26.3.4.234-lts
```

## How It Works

```
upstream:  ... --- v26.3.4.234-lts
                         |
antalya-26.3:            * (clean base branch)
                         |
ci/antalya-26.3:         * --- ci1 --- ci2 (CI rebased, PR → antalya-26.3)
                         |
                    [CI PR merged into antalya-26.3]
                         |
feature/antalya-26.3/pr-42:   * --- fix (PR → antalya-26.3)
feature/antalya-26.3/pr-99:   * --- feat (PR → antalya-26.3)
```

### Phased Pipeline

**Phase 1** — Base + CI (first `releasy run`):
1. Create base branch `antalya-26.3` from upstream `v26.3.4.234-lts`
2. Rebase CI commits onto base → `ci/antalya-26.3`
3. Open PR: CI → base (if `push: true`)
4. **Stop.** Wait for CI PR to be merged.

**Phase 2** — Features (run again after CI merged):
1. Rebase each feature onto base → `feature/antalya-26.3/<id>`
2. Open PRs: each feature → base (if `push: true` + `auto_pr: true`)

On conflict at any phase, the pipeline stops with instructions. Resolve, run `releasy continue`, then `releasy run` again.

### Branch Naming

The base/target branch is taken from `target_branch` in config when set;
otherwise it's derived from `<project>-<version>`, where `<version>` is parsed
from `--onto` (e.g. `v26.3.4.234-lts` → `26.3`; raw SHAs use the first 8 chars).

| Type | Pattern | Example |
|------|---------|---------|
| Base | `target_branch` or `<project>-<version>` | `antalya-26.3` |
| CI | `ci/<base>` | `ci/antalya-26.3` |
| Feature | `feature/<base>/<id>` | `feature/antalya-26.3/s3-disk` |
| PR Feature | `feature/<base>/pr-<N>` | `feature/antalya-26.3/pr-42` |

## Installation

```bash
pip install -e .
```

## Quick Start

1. Copy the example config and edit it:

```bash
cp config.yaml.example config.yaml
```

2. Set up authentication:

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."      # for PR discovery & creation
export RELEASY_SSH_KEY_PATH="~/.ssh/id_rsa" # optional
```

3. Run Phase 1 (base + CI):

```bash
# Using an explicit target_branch in config:
releasy run

# Or, deriving the base branch from a tag:
releasy run --onto v26.3.4.234-lts
```

4. Merge the CI PR on GitHub, then run Phase 2 (features) the same way.

## Configuration

See [`config.yaml.example`](config.yaml.example) for a fully documented template.

```yaml
# Push branches and open PRs (default: false — everything stays local)
push: true

# Reuse an existing local clone
work_dir: /path/to/ClickHouse

# Origin (required) — the repo you work against
origin:
  remote: https://github.com/Altinity/ClickHouse.git

# Upstream (optional) — omit if porting within your own repo
upstream:
  remote: https://github.com/ClickHouse/ClickHouse.git

# Project name (used for derived branch names)
project: antalya

# Target/base branch PRs are opened into.
# When set, --onto becomes optional on the CLI.
target_branch: antalya-26.3

ci:
  branch_prefix: ci/antalya
  source_branch: antalya-ci    # omit to skip CI rebase entirely
  if_exists: recreate          # skip (default) or recreate

# Static feature branches
features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    source_branch: feature/antalya-s3-disk

# PR discovery and filtering
# Set arithmetic: union(by_labels) − exclude_labels + include_prs − exclude_prs
pr_sources:
  by_labels:
    - labels: ["forward-port", "v26.3"]
      merged_only: true
      auto_pr: true

  exclude_labels: ["do-not-port"]

  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/123

  exclude_prs:
    - https://github.com/Altinity/ClickHouse/pull/789

  # Sequential PR groups: cherry-pick multiple PRs onto ONE branch and
  # open ONE combined PR. Use when later PRs depend on earlier ones.
  # AI conflict resolution runs per cherry-pick step (resolve+build+
  # commit locally); push and combined PR happen once after the last step.
  groups:
    - id: iceberg-rest
      description: "Iceberg REST catalog support"
      prs:
        - https://github.com/Altinity/ClickHouse/pull/1500
        - https://github.com/Altinity/ClickHouse/pull/1512
        - https://github.com/Altinity/ClickHouse/pull/1530
```

### Key config options

| Option | Description | Default |
|--------|-------------|---------|
| `push` | Push branches and open PRs | `false` |
| `work_dir` | Path to existing repo clone (or where to clone) | cwd |
| `origin.remote` | Origin repo URL (required) | — |
| `upstream.remote` | Upstream repo URL (optional) | — |
| `project` | Short project identifier (used in derived branch names) | — |
| `target_branch` | Explicit base branch; makes `--onto` optional | derived |
| `wip_commit_on_conflict` | On unresolved conflict: `true` = commit markers as WIP and continue; `false` = stop and leave for manual fix | `false` |
| `ci.source_branch` | Branch with CI commits; empty = skip CI rebase | — |
| `ci.if_exists` | `skip` or `recreate` when branch exists | `skip` |
| `pr_sources.by_labels[].labels` | Labels a PR must have (AND logic) | — |
| `pr_sources.by_labels[].merged_only` | Only include merged PRs | `false` |
| `pr_sources.by_labels[].auto_pr` | Auto-create PR into base branch | `false` |
| `pr_sources.by_labels[].if_exists` | Override `pr_sources.if_exists` per group | inherits |
| `pr_sources.exclude_labels` | Drop PRs carrying any of these labels | `[]` |
| `pr_sources.include_prs` | Always include these PRs (by URL) | `[]` |
| `pr_sources.exclude_prs` | Always exclude these PRs (by URL) | `[]` |
| `pr_sources.groups[].id` | Group id; becomes feature id and branch name (`feature/<base>/<id>`) | — |
| `pr_sources.groups[].prs` | Ordered list of PR URLs to cherry-pick onto a single branch and combine into one PR | — |
| `pr_sources.groups[].description` | Title text for the combined PR | id |
| `pr_sources.groups[].auto_pr` | Open a combined PR for the group | `true` |
| `pr_sources.groups[].if_exists` | Override `pr_sources.if_exists` per group | inherits |
| `pr_sources.if_exists` | When a port branch exists *locally only*: `skip` or `recreate`. Also: with `recreate`, an in-progress cherry-pick / merge / rebase at startup is auto-aborted; with `skip` the pipeline halts. Branches that already exist on the remote are always skipped. | `skip` |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `RELEASY_GITHUB_TOKEN` | GitHub PAT for PR discovery, PR creation, Project sync |
| `RELEASY_SSH_KEY_PATH` | SSH key for git operations (optional, defaults to agent) |

## CLI Reference

### Pipeline

```bash
# Run the phased pipeline (--onto optional when target_branch is in config)
releasy run [--onto <tag-or-sha>] [--work-dir <path>]

# Continue after manual conflict resolution.
# - No args: process every port in state (push + open PRs for resolved
#   ones, highlight any still-unresolved).
# - --branch <name>: just mark that one branch as resolved.
releasy continue [--branch <branch-or-feature-id>]

# Skip a conflicted branch
releasy skip --branch <branch-or-feature-id>

# Abort current run
releasy abort

# Print current state
releasy status
```

### Global options

```bash
releasy --config <path>    # config file (default: ./config.yaml)
releasy --version
```

### Feature management

```bash
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

### Release construction

```bash
releasy release \
  --upstream-tag <tag> \
  --name <branch-name> \
  [--strict] \
  [--include-skipped]
```

## Conflict Resolution

When a cherry-pick conflicts and the AI resolver doesn't fix it (or is
disabled), the pipeline **stops on the first unresolved conflict** and leaves
the working tree in the conflicted state so you can resolve it manually:

```
✗ Conflict!
  • src/Access/AccessControl.h
  • src/Access/Authentication.cpp

Pipeline stopped on unresolved conflict.
Branch feature/antalya-26.3/pr-1430 left with conflict markers
and an in-progress cherry-pick.

To resolve manually:
  cd /path/to/repo
  # edit the conflicted files, then:
  git add -A && git cherry-pick --continue
  releasy continue
  releasy run    # to continue with remaining PRs
```

`releasy continue` (no args) walks every port in state and:

- **`ok` / `skipped` / `disabled`** — leaves them alone.
- **`conflict`, now resolved** (clean tree, commit beyond base) — marks
  resolved, pushes, opens the PR.
- **`conflict`, still unresolved** — highlights it with the conflicting
  files and the `cd … && git status` hint, then moves on. Re-run
  `releasy continue` after fixing.
- **`resolved` without a PR yet** — pushes and opens the PR.

If you'd rather have releasy auto-commit conflict markers as a WIP, push
them, and open a "needs resolution" PR (the old behavior), set
`wip_commit_on_conflict: true` in `config.yaml`.

## Safety

**PRs are never opened against upstream.** When an upstream is configured, a hard safeguard in the code refuses to create any PR if the target repo matches the upstream remote.

## State Files

- `state.yaml` — pipeline phase, branch names, statuses (auto-managed)
- `STATUS.md` — human-readable status table (auto-managed)
- `config.yaml` — your configuration (gitignored, not committed)

## GitHub Project Integration

Optionally sync branch status to a GitHub Projects v2 board. This is a one-time manual setup — after that, RelEasy keeps it in sync automatically.

### Setup (one-time)

1. **Create the project:** Go to `https://github.com/orgs/<your-org>/projects` → **New project** → choose **Table** layout. Name it e.g. "RelEasy Rebase Status".

2. **Configure the Status field:** The project has a default "Status" field. Click the field header → **Edit field** → set the options to exactly:
   - `Ok`
   - `Conflict`
   - `Resolved`
   - `Skipped`
   - `Disabled`
   - `Pending`

3. **Get the project URL:** Copy the URL from the browser, e.g. `https://github.com/orgs/Altinity/projects/1` (for personal repos: `https://github.com/users/yourname/projects/1`).

4. **Token permissions:** Your `RELEASY_GITHUB_TOKEN` needs `repo` + `project` scopes (classic PAT), or "Projects" read/write permission (fine-grained PAT).

5. **Add to config:**

```yaml
push: true    # project sync only runs when push is enabled

notifications:
  github_project: https://github.com/orgs/Altinity/projects/1
```

### Alternative: auto-setup

If you have a project but it's empty, or no project yet:

```bash
releasy setup-project
```

This creates the project (if needed) and adds the Status field with the correct options. It prints the URL to add to your config.

### What RelEasy does automatically

After each state change (when `push: true`), RelEasy:
- Creates a **view (tab)** for each rebase, named after the base branch (e.g. `antalya-26.3`)
- Creates a **draft issue card** for each branch (CI + each feature)
- Sets the **Status** field to match the pipeline state
- Updates the card body with upstream commit, conflicted files, etc.
- On re-runs, replaces old cards with updated ones

One project, multiple views — each rebase gets its own tab. You don't need to create any cards or views manually.

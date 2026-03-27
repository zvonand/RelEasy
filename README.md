# RelEasy

CLI tool for managing fork rebases, feature branches, and release construction.

RelEasy automates maintaining a fork against upstream. Instead of rebasing long-lived branches (which accumulates conflicts), it creates a **base branch** from an upstream tag/commit, rebases CI and features on top, and opens PRs — all in a phased, resumable workflow.

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

Tags are parsed to extract version: `v26.3.4.234-lts` → `26.3`. Raw SHAs use the first 8 chars.

| Type | Pattern | Example |
|------|---------|---------|
| Base | `<project>-<version>` | `antalya-26.3` |
| CI | `ci/<project>-<version>` | `ci/antalya-26.3` |
| Feature | `feature/<project>-<version>/<id>` | `feature/antalya-26.3/s3-disk` |
| PR Feature | `feature/<project>-<version>/pr-<N>` | `feature/antalya-26.3/pr-42` |

The `<project>` name is derived from `ci.branch_prefix` (e.g. `ci/antalya` → `antalya`).

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
releasy run --onto v26.3.4.234-lts
```

4. Merge the CI PR on GitHub, then run Phase 2 (features):

```bash
releasy run --onto v26.3.4.234-lts
```

## Configuration

See [`config.yaml.example`](config.yaml.example) for a fully documented template.

```yaml
# Push branches and open PRs (default: false — everything stays local)
push: true

# Reuse an existing local clone
work_dir: /path/to/ClickHouse

upstream:
  remote: https://github.com/ClickHouse/ClickHouse.git

fork:
  remote: https://github.com/Altinity/ClickHouse.git

ci:
  branch_prefix: ci/antalya
  source_branch: antalya-ci    # omit to skip CI rebase entirely
  if_exists: redo              # skip (default) or redo

# Static feature branches
features:
  - id: s3-disk
    description: "Custom S3 disk improvements"
    source_branch: feature/antalya-s3-disk

# PR-based features — discovered by labels (AND logic)
pr_sources:
  - labels: ["forward-port", "v26.3"]
    merged_only: true
    auto_pr: true
    if_exists: skip
```

### Key config options

| Option | Description | Default |
|--------|-------------|---------|
| `push` | Push branches and open PRs | `false` |
| `work_dir` | Path to existing repo clone (or where to clone) | cwd |
| `ci.source_branch` | Branch with CI commits; empty = skip CI rebase | required |
| `ci.if_exists` | `skip` or `redo` when branch exists | `skip` |
| `pr_sources[].labels` | Labels a PR must have (ALL required) | — |
| `pr_sources[].merged_only` | Only include merged PRs | `false` |
| `pr_sources[].auto_pr` | Auto-create PR into base branch | `false` |
| `pr_sources[].if_exists` | `skip` or `redo` for feature branches | `skip` |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `RELEASY_GITHUB_TOKEN` | GitHub PAT for PR discovery, PR creation, Project sync |
| `RELEASY_SSH_KEY_PATH` | SSH key for git operations (optional, defaults to agent) |

## CLI Reference

### Pipeline

```bash
# Run the phased pipeline
releasy run --onto <tag-or-sha> [--work-dir <path>]

# Mark a conflict-resolved branch
releasy continue --branch <branch-or-feature-id>

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

On conflict, the pipeline stops and tells you exactly what to do:

```
✗ Conflict rebasing CI!
  • cmake/autogenerated_versions.txt

  Resolve: cd /path/to/repo
  Then: git add <files> && git rebase --continue
  When done: releasy continue --branch ci/antalya-26.3
```

For PR-based features, conflict markers are committed as a WIP and pushed (if push enabled), so someone can resolve them via the PR.

## Safety

**PRs are never opened against upstream.** A hard safeguard in the code refuses to create any PR if the target repo matches the upstream remote.

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

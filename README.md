# RelEasy

CLI tool for managing port branches, feature branches, and release construction.

RelEasy automates porting features and PRs onto a stable **base branch** (a tag/commit you pin to) inside a single repo. Instead of rebasing long-lived branches (which accumulates conflicts), each feature / PR is cherry-picked onto its own port branch and opened as a PR — all in a resumable workflow.

## TL;DR

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."

# target_branch is set in config.yaml — the base branch must already exist on origin:
releasy run
```

## How It Works

```
origin/antalya-26.3:          * (stable base branch on origin — you maintain it)
                              |
feature/antalya-26.3/pr-42:   * --- fix   (PR → antalya-26.3)
feature/antalya-26.3/pr-99:   * --- feat  (PR → antalya-26.3)
```

### Pipeline

Given an existing base branch on origin, `releasy run`:

1. Discovers PRs from `pr_sources` (labels, explicit include/exclude lists, groups).
2. For each PR / group, creates a port branch `feature/<base>/<id>` from the base.
3. Cherry-picks the PR merge commit(s) onto the port branch.
4. Pushes and opens a PR into the base (if `push: true` and `pr_sources.auto_pr: true` — the default).

On conflict, the pipeline stops with instructions. Resolve, run `releasy continue`, then `releasy run` again to resume with the remaining PRs.

### Branch Naming

The base/target branch is taken from `target_branch` in config when set;
otherwise it's derived from `<project>-<version>`, where `<version>` is parsed
from `--onto` (e.g. `26.3` → `26.3`, `v26.3.4.234-lts` → `26.3`; raw SHAs
use the first 8 chars).

`--onto` is a **naming label**, not a git ref — RelEasy never tries to fetch
or resolve it. The base branch itself must already exist on origin.

| Type | Pattern | Example |
|------|---------|---------|
| Base | `target_branch` or `<project>-<version>` | `antalya-26.3` |
| Feature | `feature/<base>/<id>` | `feature/antalya-26.3/s3-disk` |
| Origin PR feature | `feature/<base>/pr-<N>` | `feature/antalya-26.3/pr-42` |
| External PR feature | `feature/<base>/<owner>-<repo>-pr-<N>` | `feature/antalya-26.3/ClickHouse-ClickHouse-pr-12345` |

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

3. Run the pipeline:

```bash
# With an explicit target_branch in config:
releasy run

# Or, when target_branch is unset, derive the base branch name from a
# version label (the base branch must still already exist on origin —
# --onto is just used for naming, never resolved as a git ref):
releasy run --onto 26.3
```

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

# Project name (used for derived branch names)
project: antalya

# Target/base branch PRs are opened into.
# When set, --onto becomes optional on the CLI.
target_branch: antalya-26.3

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

  exclude_labels: ["do-not-port"]

  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/123
    # Cross-repo PR — fetched directly from the source repo by URL.
    # Origin keeps being the only configured remote.
    - https://github.com/ClickHouse/ClickHouse/pull/12345

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
| `project` | Short project identifier (used in derived branch names) | — |
| `target_branch` | Explicit base branch; makes `--onto` optional | derived |
| `update_existing_prs` | When a PR already exists for a port branch: `true` = reuse it and overwrite its title/body; `false` = leave it exactly as-is | `false` |
| `ai_resolve.max_iterations` | Hard cap (passed to Claude) on build attempts per conflict | `5` |
| `ai_resolve.api_retries` | Re-invoke Claude on transient Anthropic API errors (separate from `max_iterations`) | `3` |
| `ai_resolve.label` | Label attached to PRs whose conflicts Claude resolved cleanly | `ai-resolved` |
| `ai_resolve.needs_attention_label` | Label attached to draft PRs from partial-group failures | `ai-needs-attention` |
| `pr_sources.auto_pr` | Open a PR for every pushed port branch (singletons, by_labels, include_prs, groups). Requires `push: true`. | `true` |
| `pr_sources.by_labels[].labels` | Labels a PR must have (AND logic) | — |
| `pr_sources.by_labels[].merged_only` | Only include merged PRs | `false` |
| `pr_sources.by_labels[].if_exists` | Override `pr_sources.if_exists` per group | inherits |
| `pr_sources.exclude_labels` | Drop PRs carrying any of these labels | `[]` |
| `pr_sources.include_prs` | Always include these PRs (by URL) | `[]` |
| `pr_sources.exclude_prs` | Always exclude these PRs (by URL) | `[]` |
| `pr_sources.groups[].id` | Group id; becomes feature id and branch name (`feature/<base>/<id>`) | — |
| `pr_sources.groups[].prs` | Ordered list of PR URLs to cherry-pick onto a single branch and combine into one PR | — |
| `pr_sources.groups[].description` | Title text for the combined PR | id |
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

# Reconcile every port in state.
# - No args: open PRs for any clean branch that lacks one, push + open PRs
#   for newly-resolved conflicts, highlight any still-unresolved ones, and
#   refresh the GitHub Project board.
# - --branch <name>: flip that one branch from conflict to needs-review.
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
  --base-tag <tag> \
  --name <branch-name> \
  [--strict] \
  [--include-skipped]
```

## Conflict Resolution

When a cherry-pick conflicts and the AI resolver is disabled or gives
up (after exhausting `ai_resolve.max_iterations` build attempts),
RelEasy cleans up the in-progress git operation and **flags the unit
for manual review** — the pipeline keeps moving with the next unit
instead of stalling. There are two flavours of cleanup:

### Singleton PR (or first PR of a group)

The branch has no commits worth keeping, so RelEasy:

- aborts the in-progress cherry-pick,
- deletes the local port branch,
- marks the entry as **`Conflict`** in the GitHub Project (with
  the conflicted files in the card body),
- does **not** push and does **not** open a PR.

```
✗ Conflict on #1430!
  • src/Access/AccessControl.h
  • src/Access/Authentication.cpp
Dropped local branch feature/antalya-26.3/pr-1430 (AI resolver gave up; nothing to keep).
```

Resolve the source PR manually (rebase / fix it on its own) and re-run
`releasy run` to give it another shot.

### Partial group (one of the later PRs in a group fails)

The earlier picks already produced valid commits, so RelEasy:

- aborts the failed pick (the prior commits stay on the branch),
- pushes the branch,
- opens a **draft PR** labelled `ai-needs-attention` with a banner in
  the body explaining what failed,
- marks the entry as **`Conflict`** in the GitHub Project,
- does **not** attempt the remaining PRs in the group.

Pull the branch, resolve the conflict locally, push, then mark the PR
ready for review.

### `releasy continue`

The catch-all "reconcile everything" command. Walks every port in state
and:

- **`skipped`** — leaves it alone.
- **`conflict` (AI gave up)** — entries with `failed_step_index` /
  `partial_pr_count` / `rebase_pr_url` set: highlights and skips. You
  fix it manually (on the draft PR or the source PR) and re-run.
- **`conflict`, still unresolved** — highlights with the conflicting
  files and the `cd … && git status` hint, then moves on.
- **`conflict`, now resolved** (clean tree, commit beyond base) — pushes,
  opens the PR (if `auto_pr` is on), flips to `needs_review` (or
  `branch_created` if no PR was opened).
- **`branch_created`** (branch on origin, no PR yet) — pushes (if
  needed) and opens the PR. Covers the case where the previous run had
  `pr_sources.auto_pr: false` and only pushed the branch, or where an
  earlier failure prevented PR creation. AI-resolved branches also get
  the `ai-resolved` label applied. If `auto_pr` is still off, stays
  as `branch_created` and surfaces a *Create PR manually* compare-URL on
  the project board.
- **`needs_review`** — left alone (PR exists, ready for review).

Always finishes with a project-board reconciliation pass: stale draft
stubs created on earlier no-PR runs are removed and replaced by the real
PR cards.

> **Status semantics:** every successful port — clean cherry-pick or
> AI-resolved — that has a rebase PR open lands at `needs_review`. If
> the branch was pushed but no PR was opened (`pr_sources.auto_pr:
> false`, or a transient PR-creation failure), it lands at
> `branch_created`; the project board card includes a *compare* URL so
> you can open the PR with one click. Anything that needs a human to
> fix a conflict — plain cherry-pick failure or AI-gave-up — lands at
> `conflict` (the AI-gave-up flavour additionally fills in
> `failed_step_index` / `partial_pr_count` / `rebase_pr_url`, and the
> project card body explains what happened). The `ai_resolved` field on
> the entry (and the `ai-resolved` PR label) carries the "AI was
> involved" signal. Old state files with `ok` / `resolved` / `pending` /
> `disabled` / `needs_resolution` are migrated silently on load.

## PR title & labels

Rebase PRs are titled `"<Project> <version>: <subject>"` — e.g.
`"Antalya 26.3: Token Authentication and Authorization"` or
`"Stable 26.3: …"`. The version is taken from the base branch (the bit
after `<project>-`); the project name is title-cased only when it's
all lowercase, so e.g. `ClickHouse` is preserved verbatim.

The source PR's title is sanitised first to drop a leading
`<version>[<project>]:` prefix that some workflows use to encode the
backport's *original* target. So a source titled
`"26.1 Antalya: Token Authentication and Authorization"` ported onto
`antalya-26.3` becomes `"Antalya 26.3: Token Authentication and Authorization"`,
not the misleading `"… 26.1 Antalya: …"`.

Every PR RelEasy opens or updates is tagged with the `releasy` label
(auto-created on first run when `push: true`). AI-resolved PRs additionally
get the `ai_resolve.label` (default `ai-resolved`).

## Safety: PRs always target origin

Cross-repo PR URLs (`pr_sources.include_prs`, `pr_sources.groups[].prs`) are read-only sources for cherry-picks. RelEasy will only ever **create**, **update**, or **label** PRs in the repo configured as `origin`, and only ever **push branches** to that same remote. The guarantees are enforced at the lowest level:

- `create_pull_request`, `update_pull_request`, `add_label_to_pr`, and `ensure_label` all derive their target slug from `origin` on every call and pass it through `_assert_writes_target_origin`, which refuses (loudly, with a `ValueError`) to write to anything else. There is no parameter to point them at a different repo.
- The branch-push helper `force_push(repo_path, branch, config)` takes the `Config` itself and force-pushes to `config.origin.remote_name` — there is no `remote` parameter to override.
- At the start of `releasy run`, the line `PRs will be opened against <owner>/<repo> (origin)` is printed so the target is visible in the run log.

If you ever see RelEasy attempting to write to anything other than your configured origin, it's a bug — please report it.

## State Files

- `state.yaml` — pipeline phase, branch names, statuses (auto-managed)
- `STATUS.md` — human-readable status table (auto-managed)
- `config.yaml` — your configuration (gitignored, not committed)

## GitHub Project Integration

Optionally sync branch status to a GitHub Projects v2 board. This is a one-time manual setup — after that, RelEasy keeps it in sync automatically.

### Setup (one-time)

1. **Create the project:** Go to `https://github.com/orgs/<your-org>/projects` → **New project** → choose **Table** layout. Name it e.g. "RelEasy Rebase Status".

2. **Configure the Status field:** The project has a default "Status" field. Click the field header → **Edit field** → set the options to exactly:
   - `Needs Review`
   - `Branch Created`
   - `Conflict`
   - `Skipped`

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

This creates the project (if needed), reconciles the Status field options exactly to the canonical set (`Needs Review`, `Branch Created`, `Conflict`, `Skipped`), and triggers a project sync.

> **Note — destructive option reconcile:** the Status field is fully owned by RelEasy. `setup-project` drops any options that aren't in the canonical set (e.g. legacy `Ok` / `Resolved` from earlier RelEasy versions, or anything else hand-added). Cards previously sitting on a dropped option lose their Status value momentarily — the immediate `sync-project` pass that follows re-assigns them based on local state (which is itself migrated to the new vocabulary on load). If you have hand-added options you want to keep, edit `STATUS_OPTIONS` in `src/releasy/github_ops.py` to include them.

### What RelEasy does automatically

After each state change (when `push: true`), RelEasy:
- Creates a **view (tab)** for each rebase, named after the base branch (e.g. `antalya-26.3`)
- Attaches the **real PR** to the project (or a draft-issue stub for `Branch Created` ports)
- Sets the **Status** field to match the pipeline state
- Updates the card body with the base commit, conflicted files, and (for `Branch Created`) a *Open a pull request manually* compare URL
- Replaces stale draft stubs with the real PR card once a PR gets opened

One project, multiple views — each rebase gets its own tab. You don't need to create any cards or views manually.

### Recommended view setup: group by Status

To get the same grouped layout as `STATUS.md` and `releasy status` on the project board, set the view to **Group by → Status** in the GitHub UI:

1. Open the view (the tab named after your base branch).
2. Click the **⋯** menu in the view header → **Group**.
3. Pick **Status**.

This is a one-time UI setting (GitHub's Projects v2 GraphQL API doesn't expose view-config writes, so RelEasy can't set it for you). After that, your board mirrors the local layout: one section per status, ordered Conflict → Branch Created → Needs Review → Skipped.

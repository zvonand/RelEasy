# RelEasy

CLI tool for managing port branches, feature branches, and release construction.

RelEasy automates porting features and PRs onto a stable **base branch** (a tag/commit you pin to) inside a single repo. Instead of rebasing long-lived branches (which accumulates conflicts), each feature / PR is cherry-picked onto its own port branch and opened as a PR — all in a resumable workflow.

A single machine can drive **multiple ongoing porting projects in parallel** (e.g. an antalya-26.3 forward-port and an antalya-25.8 backport). Each project has its own `config.yaml` with a unique `name:`; pipeline state lives outside your repo under `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml`, and a per-project lock keeps concurrent runs of the same project serialized while runs of *different* projects truly run in parallel.

## TL;DR

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."

mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
$EDITOR config.yaml          # fill in origin remote, work_dir, push: true, …
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

1. Scaffold a config in a fresh directory (one per project):

```bash
mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
# /home/<you>/work/antalya-26.3/config.yaml
```

`releasy new` writes a minimal `config.yaml` and prints its absolute path
to stdout (everything else goes to stderr) so it composes cleanly with
shell substitution. See [`config.yaml.example`](config.yaml.example) for
the fully-documented reference of every available option.

2. Edit `config.yaml` to fill in `origin.remote`, optionally `work_dir`,
   `push: true`, `pr_sources`, `ai_resolve`, and `notifications`.

3. Set up authentication:

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."      # for PR discovery & creation
export RELEASY_SSH_KEY_PATH="~/.ssh/id_rsa" # optional
```

4. Run the pipeline:

```bash
# With an explicit target_branch in config:
releasy run

# Or, when target_branch is unset, derive the base branch name from a
# version label (the base branch must still already exist on origin —
# --onto is just used for naming, never resolved as a git ref):
releasy run --onto 26.3
```

## Multiple projects in parallel

Each `config.yaml` carries a required `name:` slug. That name keys a
per-project state file under `${XDG_STATE_HOME:-~/.local/state}/releasy/`
(overridable with `$RELEASY_STATE_DIR`) and a per-project lockfile next
to it, so two projects with different names can run truly concurrently
on the same machine:

```bash
# Project A
mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
$EDITOR config.yaml

# Project B
mkdir -p ~/work/antalya-25.8 && cd ~/work/antalya-25.8
releasy new --target-branch antalya-25.8 --project antalya
$EDITOR config.yaml

# Two terminals, two pipelines running concurrently:
(cd ~/work/antalya-26.3 && releasy run) &
(cd ~/work/antalya-25.8 && releasy run) &

# See everything releasy knows about on this machine:
releasy list
# Name           | Phase       | Features          | Last run            | Config
# antalya-26.3   | ports_done  | 3 ok / 0 conflict | 2026-04-20 08:35Z   | ~/work/antalya-26.3/config.yaml
# antalya-25.8   | init        | 0 ok / 0 conflict | —                   | ~/work/antalya-25.8/config.yaml
```

Two `releasy` invocations against the **same** project (same `name:`)
serialize on the project lock — the second one prints the holder's PID,
host, and command and exits non-zero. Two invocations against
**different** projects don't contend at all.

> **One work_dir per project.** RelEasy does not police shared
> `work_dir` settings between projects — git itself isn't safe with two
> processes mutating the same checkout. Give each project its own clone
> directory.

If you move or rename a config, the next command will trip an
ownership-collision check (the state file remembers the absolute path
of the config that owns it). Run `releasy adopt` from the new location
to rebind state to the new path.

## Configuration

See [`config.yaml.example`](config.yaml.example) for a fully documented template.

```yaml
# Unique slug for this project on this machine (required).
# Keys the per-project state file under
#   ${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml
name: antalya-26.3

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
# Set arithmetic:
#   union(by_labels)
#   − exclude_labels − exclude_authors
#   ∩ (include_authors when set)
#   + include_prs − exclude_prs
# include_prs always wins: it bypasses label and author filters.
pr_sources:
  by_labels:
    - labels: ["forward-port", "v26.3"]
      merged_only: true

  exclude_labels: ["do-not-port"]

  # Filter by PR author (GitHub login, case-insensitive).
  # include_authors is an allowlist — only PRs by these users are kept.
  # exclude_authors is a denylist.
  # include_authors: ["alice", "bob"]
  exclude_authors: ["dependabot[bot]"]

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
| `name` | Unique slug identifying this project on this machine (required). Keys `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml`. Allowed: `A-Z a-z 0-9 . _ -` (1-64 chars). | — |
| `push` | Push branches and open PRs | `false` |
| `work_dir` | Path to existing repo clone (or where to clone) | cwd |
| `origin.remote` | Origin repo URL (required) | — |
| `project` | Short project identifier (used in derived branch names) | — |
| `target_branch` | Explicit base branch; makes `--onto` optional | derived |
| `sequential` | Process the merged-time-sorted PR queue one PR per `releasy run` / `releasy continue` invocation. Each invocation requires the previously opened rebase PR to have been merged into `target_branch` before porting the next one. Incompatible with `pr_sources.groups`. | `false` |
| `update_existing_prs` | When a PR already exists for a port branch: `true` = reuse it and overwrite its title/body; `false` = leave it exactly as-is | `false` |
| `ai_resolve.max_iterations` | Hard cap (passed to Claude) on build attempts per conflict | `5` |
| `ai_resolve.api_retries` | Re-invoke Claude on transient Anthropic API errors (separate from `max_iterations`) | `3` |
| `ai_resolve.label` | Label attached to PRs whose conflicts Claude resolved cleanly | `ai-resolved` |
| `ai_resolve.needs_attention_label` | Label attached to draft PRs from partial-group failures | `ai-needs-attention` |
| `ai_resolve.prompt_file` | Prompt template used when AI resolves a conflicted **cherry-pick** | `prompts/resolve_conflict.md` |
| `ai_resolve.merge_prompt_file` | Prompt template used when AI resolves a conflicted **merge** during `releasy refresh` | `prompts/resolve_merge_conflict.md` |
| `pr_sources.auto_pr` | Open a PR for every pushed port branch (singletons, by_labels, include_prs, groups). Requires `push: true`. | `true` |
| `pr_sources.by_labels[].labels` | Labels a PR must have (AND logic) | — |
| `pr_sources.by_labels[].merged_only` | Only include merged PRs | `false` |
| `pr_sources.by_labels[].if_exists` | Override `pr_sources.if_exists` per group | inherits |
| `pr_sources.exclude_labels` | Drop PRs carrying any of these labels | `[]` |
| `pr_sources.include_authors` | Allowlist of GitHub logins (case-insensitive); when set, only PRs by these authors are kept. Bypassed by `include_prs`. | `[]` |
| `pr_sources.exclude_authors` | Drop PRs by these GitHub logins (case-insensitive). Bypassed by `include_prs`. | `[]` |
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
| `RELEASY_STATE_DIR` | Override the directory holding per-project state and lock files. Defaults to `${XDG_STATE_HOME:-~/.local/state}/releasy`. Useful for tests / CI runners. |

## CLI Reference

Every command honours the global options below. Run `releasy <command> --help`
for the authoritative option list — this section summarises what each command
does and when to reach for it.

### Global options

| Option | Description | Default |
|--------|-------------|---------|
| `--config <path>` | Path to `config.yaml` | `./config.yaml` |
| `--version` | Print version and exit | — |

### At a glance: which command does what

The three "doing" commands look similar but operate on different things —
this matrix is the quickest way to pick the right one.

| | `run` | `continue` | `refresh` |
|--|:-----:|:----------:|:---------:|
| Discovers new PRs from `pr_sources` (labels, include_prs, groups) | ✅ | — | — |
| Creates new port branches (cherry-pick onto `origin/<base>`) | ✅ | — | — |
| Opens new rebase PRs | ✅ for new ports | ✅ for branches that missed PR creation last time | — |
| AI-resolves **cherry-pick** conflicts (initial port) | ✅ | — | — |
| AI-resolves **merge** conflicts (target branch moved on) | — | — | ✅ |
| Iterates entries already in the project state file | only to skip / ensure-PR | ✅ all of them | ✅ all tracked PRs |
| Mutates your local work-dir | ✅ (cherry-picks) | ✅ (push only) | ✅ (merges) |
| Pushes to origin | ✅ | ✅ | ✅ (only merge commits, on conflict-resolution) |

In one-line summaries:

- **`run`** — *"do new work."* Walks `pr_sources`, discovers PRs, creates port
  branches, cherry-picks, opens rebase PRs.
- **`continue`** — *"I fixed something by hand; reconcile state."* Walks
  the project state file: pushes / opens any PRs that should have been
  created but weren't (e.g. previous run had `auto_pr: false`, or a
  conflict you've now manually resolved in the work-dir), refreshes the
  project board. No git ops beyond `push` + status checks.
- **`refresh`** — *"keep open PRs current with the moved-on base."* Walks
  the project state file: for each tracked PR, attempts
  `git merge origin/<base>` into the PR branch and AI-resolves any
  conflicts. Doesn't open new PRs, doesn't discover new sources.

> **Why both `run` and `continue`?** `run` only acts on entries it's
> cherry-picking *right now*. If you fix a conflict by hand later in the
> work-dir, `run` either skips your branch (`if_exists: skip`) or
> **deletes your manual fix** and rebuilds from base (`if_exists:
> recreate`). `continue` is the safe "preserve my work, just push + open
> the PR" command.

The remaining commands — `skip`, `abort`, `status`, `setup-project`,
`sync-project`, `release`, `feature *` — never touch git history; they're
state-only / project-board / config helpers (detailed below).

### Pipeline lifecycle

The five commands you'll touch on a regular release cycle.

#### `releasy run` — port PRs onto the base branch

The main pipeline. Discovers PRs from `pr_sources`, creates a port branch
per PR/group from `origin/<base_branch>`, cherry-picks the merge commit(s),
and (when `push: true` and `pr_sources.auto_pr: true`) opens a PR per port
into the base. AI-resolves **cherry-pick** conflicts inline when
`ai_resolve.enabled` is on. Unresolved conflicts are dropped (singletons
/ first-of-group) or opened as draft PRs labelled `ai-needs-attention`
(partial groups), and the pipeline keeps moving.

For entries that already produced a branch on origin, `run` skips the
cherry-pick and just makes sure a PR exists — it does **not** re-port,
re-AI-resolve, or merge any moved-on target branch into them. Use
[`refresh`](#releasy-refresh--keep-tracked-prs-current-with-the-target-branch)
for the latter.

```bash
releasy run [--onto <tag-or-sha>] [--work-dir <path>]
            [--resolve-conflicts | --no-resolve-conflicts]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Version label used to derive `<project>-<version>` if `target_branch` is unset. Just a string — never resolved as a git ref. | from `target_branch` |
| `--work-dir <path>` | Working directory for git operations (overrides config `work_dir`). | from config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Toggle the AI resolver. The flag is a kill-switch: AI runs only if both this *and* `ai_resolve.enabled` are true. | on |

Exit code: `1` if any port ended up in `conflict` status, `0` otherwise.

#### `releasy refresh` — keep tracked PRs current with the target branch

Strictly a maintenance pass — **never opens new PRs, never creates new
branches, never discovers new PR sources, never re-runs cherry-picks**.
Only operates on entries already in the project state file. For each tracked PR
with a branch + rebase PR URL, fetches latest tips, attempts
`git merge --no-ff origin/<base_branch>` into the PR branch, and:

- **clean merge** — leaves the PR alone (resets local back; if you want
  a fresh merge commit pushed, use GitHub's *Update branch* button),
- **conflict + AI resolves it** — pushes the resolved merge commit,
  preserves status (and promotes any prior `conflict` back to
  `needs_review`), sets `ai_resolved`,
- **conflict + AI gives up / disabled** — aborts the merge, hard-resets
  the local branch back to its original tip, marks the entry `conflict`
  in state + project board.

Uses a separate prompt template (`ai_resolve.merge_prompt_file`) but the
same Claude invocation machinery as `run`. Suitable for a cron / CI
loop.

```bash
releasy refresh [--work-dir <path>]
                [--resolve-conflicts | --no-resolve-conflicts]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--work-dir <path>` | Working directory for git operations. | from config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Toggle the AI resolver. With `--no-resolve-conflicts`, conflicting PRs are flagged in state without an automatic fix attempt. | on |

Exit code: `1` if any PR ended up in `conflict` status, `0` otherwise.

#### `releasy continue` — reconcile state after a manual fix

Catch-all "finish whatever can be finished" command. **Doesn't discover
new PRs, doesn't cherry-pick, doesn't merge anything in.** Just walks
every port already in state and acts on each:

- `skipped` — left alone.
- `conflict` (AI gave up) with `failed_step_index` / `partial_pr_count`
  / `rebase_pr_url` set — highlighted; user must act on the draft PR or
  source PR, then re-run.
- `conflict`, branch now clean (resolved manually in the work-dir) —
  pushes, opens the PR (if `auto_pr` is on), flips to `needs_review`.
- `conflict`, still unresolved — highlighted with conflict files and a
  `cd … && git status` hint; left alone.
- `branch_created` (branch on origin, no PR yet) — pushes (if needed)
  and opens the PR.
- `needs_review` — left alone (PR exists).

Always finishes with a project-board reconciliation pass.

Reach for this when you've done something by hand (resolved a conflict in
the work-dir, opened/closed a PR on GitHub, or just want the project
board to catch up) and you want the next `run` / `refresh` to start from
a clean view of the world.

```bash
releasy continue [--branch <branch-or-feature-id>] [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--branch <name>` | Operate on a single branch / feature ID — flips that one entry from `conflict` to `needs_review` instead of running the full reconciliation pass. | run full pass |
| `--work-dir <path>` | Working directory for git operations. | from config / cwd |

Exit code: `1` if any port still has unresolved conflicts (full-pass
mode) or if the targeted branch couldn't be marked resolved.

#### Sequential mode

When `sequential: true` is set in `config.yaml`, both `releasy run` and
`releasy continue` (without `--branch`) switch to a strict one-PR-at-a-
time pipeline. The discovered PR queue is sorted by `merged_at` (same
order as the default mode) and processed exactly one entry per
invocation:

1. **First invocation** — port the earliest-merged PR: cherry-pick onto
   the current `origin/<target_branch>` (resolving conflicts via
   `ai_resolve` if enabled), push, open the rebase PR, and exit.
2. **Manual review** — you wait for CI, review, approve, and merge that
   PR into `target_branch` yourself.
3. **Next invocation** — `releasy continue` (or `releasy run`) checks
   GitHub:
   - Previous rebase PR **merged** → marks the entry as `merged` in
     state, re-fetches `origin`, and ports the next PR off the now
     up-to-date base. Stops again after opening the new PR.
   - Previous rebase PR **not merged** (still open / closed without
     merge / API lookup failed) → exits with status `1` and changes
     nothing. Re-run after merging the in-flight PR.
4. Repeat until the queue is empty (or a port hits a conflict the AI
   can't resolve, in which case the run stops in `conflict` state and
   the next invocation refuses to advance until you fix it manually and
   call `releasy continue --branch <id>`).

Constraints:

- **Incompatible with `pr_sources.groups`** — config load fails with a
  clear error. Use `include_prs` instead (or split the group into
  individual PRs).
- Requires `target_branch:` in config (no `--onto` derivation in
  sequential mode for `releasy continue`).
- The `merged` status is reflected on the GitHub Project board too
  (re-run `releasy setup-project` once to provision the new option).

```bash
# First port:
releasy run

# (review, approve, merge the opened PR on GitHub, then:)
releasy continue
```

#### `releasy skip` — drop a conflicted port from this run

Marks a port branch as `skipped` so subsequent `continue` / project-sync
passes ignore it. Doesn't touch the branch on disk or on origin.

```bash
releasy skip --branch <branch-or-feature-id>
```

| Option | Description |
|--------|-------------|
| `--branch <name>` (required) | Branch name or feature ID to skip. |

#### `releasy abort` — abort the current run

Persists the current state, but leaves all branches and PRs exactly as
they are. There's no "undo" for ports that already pushed; this is for
telling RelEasy "stop tracking this run as in-progress" without rolling
anything back.

```bash
releasy abort
```

### Inspection

#### `releasy status` — print current pipeline state

Renders one rich-text sub-table per status section (in
[`STATUS_DISPLAY_ORDER`](src/releasy/state.py)) so the highest-attention
entries (conflicts) surface at the top. No git operations, no network
calls — just reads the project state file and prints.

```bash
releasy status
```

### Multi-project ergonomics

#### `releasy new` — scaffold a fresh project config

Writes a minimal `config.yaml` from the bundled template and prints its
absolute path on stdout (everything else goes to stderr) so it
composes:

```bash
cd $(dirname "$(releasy new --target-branch antalya-25.8 --project antalya)")
```

```bash
releasy new [--name <slug>] [--target-branch <branch>] [--project <id>] [--out <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--name <slug>` | Project name. Validated against `[A-Za-z0-9._-]{1,64}`. | auto-generated |
| `--target-branch <branch>` | Seeds `target_branch:` in the new config. Also drives the auto-generated name (`<target-branch>-<6hex>`) when `--name` is omitted. | empty |
| `--project <id>` | Seeds `project:` in the new config. | empty |
| `--out <path>` | Where to write the new file. Refuses to overwrite an existing file. | `./config.yaml` |

When `--name` is omitted, the new project gets a 6-hex suffix from a
CSPRNG so back-to-back `releasy new --target-branch X` calls produce
distinct names.

#### `releasy list` (alias `releasy ls`) — every project on this machine

Walks the state directory and renders one row per project: name, phase,
feature counts (ok / conflict, plus skipped when present), last-run
timestamp, and the absolute path of the config file that owns the
state.

```bash
releasy list
```

#### `releasy where` — print the state-file path for the current config

```bash
releasy where
# /home/<you>/.local/state/releasy/antalya-26.3.state.yaml
```

#### `releasy adopt` — rebind state to the current config

If you move or rename a `config.yaml`, the next mutating command will
trip an ownership-collision check (the state file remembers the
absolute path of the config that owned it). Run `releasy adopt` from
the new location to rebind state to the current config; the old path
is appended to a small history list for audit.

```bash
releasy adopt
```

If no state file exists yet, `releasy adopt` creates an empty one — so
it doubles as "register this config without doing anything else".

### Project board sync

Both commands are no-ops unless `notifications.github_project` is set
and `RELEASY_GITHUB_TOKEN` has the `project` scope.

#### `releasy setup-project` — create / verify the GitHub Project

If `notifications.github_project` is set, verifies the project,
reconciles its `Status` field options to the canonical set
(`Needs Review`, `Branch Created`, `Conflict`, `Skipped`) — dropping
any others — and provisions the `AI Cost` number field if it isn't
already there. If unset, creates a new project, prints the URL to add
to config, and triggers a project sync.

```bash
releasy setup-project
```

> **Destructive:** the Status field is fully owned by RelEasy. Any
> non-canonical options (legacy `Ok` / `Resolved`, or anything else
> hand-added) get dropped. Cards previously sitting on a dropped option
> are immediately re-synced to the right Status based on local state.

#### `releasy sync-project` — push local state to the project board

Reads the project state file and reconciles every known feature with
the configured project: attaches missing PR cards, refreshes existing
ones, updates the Status field. Use this after editing state by hand,
after rotating tokens, or right after wiring up a new project URL on an
in-flight rebase. No git operations, no PRs — only the board.

```bash
releasy sync-project
```

Exit code: `1` if sync was skipped (no project / no token / unparseable
URL) or any item failed to sync, `0` otherwise.

### Release construction

#### `releasy release` — build a release branch from a tag

Creates a release base branch from `--base-tag` and merges every
finished port (`needs_review`, optionally `skipped`) onto it. Useful
after a `run` cycle is fully reconciled.

```bash
releasy release \
  --base-tag <tag> \
  --name <branch-name> \
  [--strict] \
  [--include-skipped] \
  [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--base-tag <tag>` (required) | Tag/ref to base the release on. Must be present locally or fetchable from origin. | — |
| `--name <branch>` (required) | Release branch name to create. | — |
| `--strict` | Abort if any enabled feature is not in `needs_review`. | off |
| `--include-skipped` | Include `skipped` features in the release. | off |
| `--work-dir <path>` | Working directory for git operations. | from config / cwd |

### Feature management

`features:` in `config.yaml` lists static port targets — branches that
already exist somewhere and need to be re-applied onto each base. PR
sources (`pr_sources.*`) are the dynamic counterpart; this group manages
the static list.

```bash
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

| Subcommand | Description |
|------------|-------------|
| `feature add` | Append a new entry to `features:` in `config.yaml`. Requires `--id`, `--source-branch`, `--description`. |
| `feature enable` | Set `enabled: true` on a feature (runs participate in `releasy run`). Requires `--id`. |
| `feature disable` | Set `enabled: false` (skipped by `releasy run`). Requires `--id`. |
| `feature remove` | Delete the feature from `config.yaml`. Doesn't touch any branches. Requires `--id`. |
| `feature list` | Print the configured features grouped by enabled / disabled. |

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

### After conflicts: which command to run

| You want to… | Run |
|--------------|-----|
| Re-attempt a source PR you just fixed manually | [`releasy run`](#releasy-run--port-prs-onto-the-base-branch) |
| Mark a manually-resolved port branch as done (open PR + flip status) | [`releasy continue`](#releasy-continue--reconcile-state-after-a-manual-fix) |
| Flag an open rebase PR that's started conflicting with the moved-on target branch | [`releasy refresh`](#releasy-refresh--keep-tracked-prs-current-with-the-target-branch) |
| Drop a port from this run entirely | [`releasy skip`](#releasy-skip--drop-a-conflicted-port-from-this-run) |
| Force-resync the GitHub Project board to current state | [`releasy sync-project`](#releasy-sync-project--push-local-state-to-the-project-board) |

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
> involved" signal.

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

## Files RelEasy reads & writes

- `config.yaml` — your per-project configuration. The user provides
  this; `releasy new` scaffolds it. Gitignored by default; nothing
  prevents you from versioning it elsewhere.
- `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml` — the
  per-project pipeline state file (phase, branch names, statuses,
  AI cost, source PR metadata). Auto-managed; not user-editable in
  normal operation. Override the parent dir with `$RELEASY_STATE_DIR`.
- `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.lock` — POSIX
  advisory lock used to serialize concurrent invocations on the same
  project. Auto-removed on clean shutdown; harmless if a crashed run
  leaves one behind (next run reclaims it).

The state file additionally remembers the absolute path of the
`config.yaml` that owns it, so `releasy list` can show the back-link
and so an accidentally-copied config (with the same `name:`) trips a
clear ownership-collision error instead of silently sharing state.

There is **no** `STATUS.md` anymore — `releasy status` renders the same
information directly to the terminal as a rich-text table.

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

This creates the project (if needed), reconciles the Status field options exactly to the canonical set (`Needs Review`, `Branch Created`, `Conflict`, `Skipped`), provisions the `AI Cost` number field (see below), and triggers a project sync.

> **Note — destructive option reconcile:** the Status field is fully owned by RelEasy. `setup-project` drops any options that aren't in the canonical set (e.g. legacy `Ok` / `Resolved` from earlier RelEasy versions, or anything else hand-added). Cards previously sitting on a dropped option lose their Status value momentarily — the immediate `sync-project` pass that follows re-assigns them based on local state (which is itself migrated to the new vocabulary on load). If you have hand-added options you want to keep, edit `STATUS_OPTIONS` in `src/releasy/github_ops.py` to include them.

### What RelEasy does automatically

After each state change (when `push: true`), RelEasy:
- Creates a **view (tab)** for each rebase, named after the base branch (e.g. `antalya-26.3`)
- Attaches the **real PR** to the project (or a draft-issue stub for `Branch Created` ports)
- Sets the **Status** field to match the pipeline state
- Updates the **AI Cost** number field (USD) with the cumulative Anthropic
  bill RelEasy ran up resolving conflicts on this entry — `0` for cards
  the resolver never touched, monotonically increasing as later
  cherry-pick / `releasy refresh` merges spend more
- Seeds the **Assignee Dev** single-select field on freshly-created cards
  with the source PR's author (the first PR's author for groups), looked
  up via `notifications.assignee_dev_login_map`. Subsequent syncs never
  overwrite the value, so reassignments by humans on the board persist.
  **Assignee QA** is left empty for the QA team to fill in by hand.
- Updates the card body with the base commit, conflicted files, and (for `Branch Created`) a *Open a pull request manually* compare URL
- Replaces stale draft stubs with the real PR card once a PR gets opened

The `AI Cost` field is a `Number` field auto-provisioned on the project
the first time `releasy setup-project` or any sync runs against it. It
holds the sum of `total_cost_usd` reported by Claude across every
resolve invocation that touched the entry — both successful and failed
attempts (a failed turn is still billed). The same value is persisted
in the per-project state file as `ai_cost_usd`, so re-syncing a board after restoring
state keeps the column accurate.

One project, multiple views — each rebase gets its own tab. You don't need to create any cards or views manually.

### Recommended view setup: group by Status

To get the same grouped layout as `releasy status` on the project board, set the view to **Group by → Status** in the GitHub UI:

1. Open the view (the tab named after your base branch).
2. Click the **⋯** menu in the view header → **Group**.
3. Pick **Status**.

This is a one-time UI setting (GitHub's Projects v2 GraphQL API doesn't expose view-config writes, so RelEasy can't set it for you). After that, your board mirrors the local layout: one section per status, ordered Conflict → Branch Created → Needs Review → Skipped.

### Showing the AI Cost column

`releasy setup-project` provisions the `AI Cost` number field on the
project, and every sync writes a value to each card (`0` for untouched
cards, the cumulative USD bill otherwise). Projects v2 does **not**
auto-add newly-created fields as visible columns in pre-existing
views, so you need to flip it on once per view:

1. Open the view (the tab named after your base branch).
2. Click the **⋯** menu in the view header → **Fields**.
3. Toggle **AI Cost** on.

Same Projects v2 API limitation as `Group by → Status` — RelEasy can't
set view-level field visibility for you. The data is always there: open
any card and the field shows up in its side panel even when it isn't a
column.

### Assignee Dev / Assignee QA

`releasy setup-project` provisions two extra single-select fields the
**first time** it sees the board:

- **`Assignee Dev`** — the developer responsible for the port. RelEasy
  seeds it once with the source PR's author when a card is first
  created, mapping the GitHub login through
  `notifications.assignee_dev_login_map` to one of
  `notifications.assignee_dev_options`. Logins not in the map (and
  groups whose first PR has no known author) leave the field empty.
- **`Assignee QA`** — the QA owner. Always empty by default; the QA team
  fills it in manually. The option list includes the special
  `Verified by Dev` value to flag entries that don't need a separate
  QA pass.

Both fields' option lists come from `config.yaml`
(`notifications.assignee_dev_options` / `assignee_qa_options`). On a
freshly created board RelEasy provisions exactly the listed options;
on subsequent runs RelEasy **never edits the option list** — that's
the user's job in the GitHub UI, so manual additions / removals are
preserved across re-runs. To add a new team member, edit the option
list in the GitHub project settings (and, if Dev, also add the
login → label entry to `assignee_dev_login_map` so the next batch of
cards picks them up automatically).

Like `AI Cost`, both fields exist on every card but Projects v2 doesn't
auto-add new fields to existing views — flip them on once per view via
**⋯ → Fields**.

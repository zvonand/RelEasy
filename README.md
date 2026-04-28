# RelEasy

CLI tool for managing port branches, feature branches, and release construction.

RelEasy automates porting features and PRs onto a stable **base branch** (a tag/commit you pin to) inside a single repo. Instead of rebasing long-lived branches (which accumulates conflicts), each feature / PR is cherry-picked onto its own port branch and opened as a PR — all in a resumable workflow.

A single machine can drive **multiple ongoing porting projects in parallel** (e.g. an antalya-26.3 forward-port and an antalya-25.8 backport). Each project has its own `config.yaml` with a unique `name:`; pipeline state lives outside your repo under `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml`, and a per-project lock keeps concurrent runs of the same project serialized while runs of *different* projects truly run in parallel.

Each project spreads across three files with clear responsibilities:

| File | What | Lifetime |
|------|------|----------|
| `config.yaml` | Stable infrastructure: origin remote, work_dir, target branch, AI settings, notifications, `pr_policy`. | Edited once at setup, rarely touched. |
| `<name>.session.yaml` | Per-effort source data: `features:` list, `pr_sources:` selectors (labels, include/exclude PR URLs, groups, author filters). Lives next to `config.yaml` by default; point elsewhere with `session_file:` in config or `--session-file` on the CLI. | Edited between runs as your target work changes. |
| `<name>.state.yaml` | Runtime progress managed by RelEasy. Lives under `${XDG_STATE_HOME:-~/.local/state}/releasy/`. | Never edited by hand. |

`releasy --stateless` (e.g. `address-review --stateless`) loads `config.yaml` but skips the session and state files — useful for one-off runs where CLI flags supply everything session data would otherwise provide.

## TL;DR

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."

mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
$EDITOR config.yaml                     # origin remote, work_dir, push: true, …
$EDITOR antalya-26.3.session.yaml       # features + pr_sources
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

1. Discovers PRs from `pr_sources` in the session file (labels, explicit include/exclude lists, groups).
2. For each PR / group, creates a port branch `feature/<base>/<id>` from the base.
3. Cherry-picks the PR merge commit(s) onto the port branch.
4. Pushes and opens a PR into the base (if `push: true` and `pr_policy.auto_pr: true` — the default).

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

1. Scaffold a config + session pair in a fresh directory (one per project):

```bash
mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
# /home/<you>/work/antalya-26.3/config.yaml
# /home/<you>/work/antalya-26.3/antalya-26.3.session.yaml
```

`releasy new` writes a minimal `config.yaml` plus a sibling
`<name>.session.yaml` and prints the config's absolute path to stdout
(everything else goes to stderr) so it composes cleanly with shell
substitution. See [`config.yaml.example`](config.yaml.example) and
[`session.yaml.example`](session.yaml.example) for fully-documented
references.

2. Edit `config.yaml` to fill in `origin.remote`, optionally `work_dir`,
   `push: true`, `pr_policy`, `ai_resolve`, and `notifications`. Edit
   the session file to declare `features:` and `pr_sources:`.

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

See [`config.yaml.example`](config.yaml.example) and
[`session.yaml.example`](session.yaml.example) for fully documented
templates.

### config.yaml (stable infrastructure)

```yaml
# Unique slug for this project on this machine (required).
# Keys the per-project state file under
#   ${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml
# and the session file at
#   <config-dir>/<name>.session.yaml
name: antalya-26.3

# Optional override for the session file path. Relative paths resolve
# against this config's directory. CLI `--session-file` always wins.
# session_file: sessions/antalya-26.3.session.yaml

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

# Policy knobs applied to every discovered PR / group.
# All fields optional; shown below are the defaults.
# pr_policy:
#   if_exists: skip
#   auto_pr: true
#   retry_failed: true
#   recreate_closed_prs: false
```

### <name>.session.yaml (per-effort source data)

```yaml
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
  # `sort:` controls cherry-pick order — "listed" (default) walks the
  # `prs:` list, "merged_at" sorts by GitHub merge timestamp ascending.
  groups:
    - id: iceberg-rest
      description: "Iceberg REST catalog support"
      # sort: merged_at
      prs:
        - https://github.com/Altinity/ClickHouse/pull/1500
        - https://github.com/Altinity/ClickHouse/pull/1512
        - https://github.com/Altinity/ClickHouse/pull/1530
```

### Key options

Options in the table below live in **config.yaml** unless marked
*(session)*, which means they live in the session file.

| Option | Description | Default |
|--------|-------------|---------|
| `name` | Unique slug identifying this project on this machine (required). Keys `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml` and (by default) `<config-dir>/<name>.session.yaml`. Allowed: `A-Z a-z 0-9 . _ -` (1-64 chars). | — |
| `session_file` | Override the session file path. Relative paths resolve against the config's directory. | `<config-dir>/<name>.session.yaml` |
| `push` | Push branches and open PRs | `false` |
| `work_dir` | Path to existing repo clone (or where to clone) | cwd |
| `origin.remote` | Origin repo URL (required) | — |
| `project` | Short project identifier (used in derived branch names) | — |
| `target_branch` | Explicit base branch; makes `--onto` optional | derived |
| `sequential` | Process the merged-time-sorted PR queue one PR per `releasy run` / `releasy continue` invocation. Each invocation requires the previously opened rebase PR to have been merged into `target_branch` before porting the next one. Incompatible with session `pr_sources.groups`. | `false` |
| `update_existing_prs` | When a PR already exists for a port branch: `true` = reuse it and overwrite its title/body; `false` = leave it exactly as-is | `false` |
| `ai_resolve.max_iterations` | Hard cap (passed to Claude) on build attempts per conflict | `5` |
| `ai_resolve.api_retries` | Re-invoke Claude on transient Anthropic API errors (separate from `max_iterations`) | `3` |
| `ai_resolve.label` | Label attached to PRs whose conflicts Claude resolved cleanly | `ai-resolved` |
| `ai_resolve.needs_attention_label` | Label attached to draft PRs from partial-group failures | `ai-needs-attention` |
| `ai_resolve.prompt_file` | Prompt template used when AI resolves a conflicted **cherry-pick** | `prompts/resolve_conflict.md` |
| `ai_resolve.merge_prompt_file` | Prompt template used when AI resolves a conflicted **merge** during `releasy refresh` | `prompts/resolve_merge_conflict.md` |
| `ai_changelog.enabled` | Use Claude to compose a single user-facing CHANGELOG entry for multi-PR groups (drops intermediate fix-ups, refactors, and other non-user-visible churn). Singletons always reuse the source PR's entry verbatim — no Claude call. | `false` |
| `ai_changelog.command` | Claude executable used for synthesis. | `claude` |
| `ai_changelog.prompt_file` | Prompt template for changelog synthesis. | `prompts/synthesize_changelog.md` |
| `ai_changelog.timeout_seconds` | Per-call timeout for the synthesis subprocess. | `300` |
| `ai_changelog.max_pr_body_chars` | Per-PR body trim before being inlined into the prompt. | `3000` |
| `review_response.trusted_reviewers` | Allowlist of GitHub logins whose comments the AI will see. Combined with `--reviewer` on the CLI; if both are empty the command refuses to run. | `[]` |
| `review_response.reply_to_non_addressable` | Post an in-thread reply (with a bot footer) on every comment the AI classifies as non-actionable. CLI override: `--reply` / `--no-reply`. | `true` |
| `review_response.post_summary_comment` | Additionally post one top-level summary comment on the PR when done. Distinct from per-comment replies; both can be on. | `false` |
| `review_response.prompt_file` | Prompt template for review-response runs. | `prompts/address_review.md` |
| `review_response.max_iterations` | Hard cap on build attempts the AI may make per address-review run. | `15` |
| `review_response.timeout_seconds` | Per-invocation Claude timeout. | `7200` |
| `analyze_fails.command` | Claude executable used by `analyze-fails`. | `claude` |
| `analyze_fails.prompt_file` | Prompt template for `analyze-fails`. | `prompts/analyze_fails.md` |
| `analyze_fails.timeout_seconds` | Per-invocation Claude timeout for `analyze-fails`. | `7200` |
| `analyze_fails.max_iterations` | Build-attempt cap per failed test the AI investigates. | `6` |
| `analyze_fails.max_prs_per_run` | Cap on tracked PRs iterated when `--pr` is omitted (0 = no cap). | `0` |
| `analyze_fails.flaky_elsewhere_threshold` | A failed test seen on this many OTHER tracked PRs is flagged to Claude as a probable master-side flake. `0` disables the heuristic. | `2` |
| `analyze_fails.flaky_check_prs` | Cap on PRs scanned to build the flaky-elsewhere map. | `12` |
| `analyze_fails.post_comment_to_pr` | Post a top-level summary comment on each processed PR (per-shard outcomes, AI narration, commit + push status). | `true` |
| `pr_policy.auto_pr` | Open a PR for every pushed port branch (singletons, by_labels, include_prs, groups). Requires `push: true`. | `true` |
| `pr_policy.if_exists` | Default for session-file source entries that don't set their own: when a port branch exists *locally only*, `skip` or `recreate`. With `recreate`, an in-progress cherry-pick / merge / rebase at startup is auto-aborted; with `skip` the pipeline halts. Branches that already exist on the remote are always skipped. | `skip` |
| `pr_policy.retry_failed` | When a PR unit has a `conflict` entry in state from a previous run: `true` discards the existing local / remote port branch and re-runs the cherry-pick from base; `false` leaves the entry exactly as-is. Overridable per-invocation with `--retry-failed` / `--no-retry-failed` on `releasy run`. | `true` |
| `pr_policy.recreate_closed_prs` | When `true`, if state has a `rebase_pr_url` and that GitHub PR is closed (not merged), allocate `<canonical>-1`, `-2`, … for the port branch and cherry-pick + open a fresh PR. Off by default. | `false` |
| `pr_sources.by_labels[].labels` *(session)* | Labels a PR must have (AND logic) | — |
| `pr_sources.by_labels[].merged_only` *(session)* | Only include merged PRs | `false` |
| `pr_sources.by_labels[].if_exists` *(session)* | Override `pr_policy.if_exists` per entry | inherits |
| `pr_sources.exclude_labels` *(session)* | Drop PRs carrying any of these labels | `[]` |
| `pr_sources.include_authors` *(session)* | Allowlist of GitHub logins (case-insensitive); when set, only PRs by these authors are kept. Bypassed by `include_prs`. | `[]` |
| `pr_sources.exclude_authors` *(session)* | Drop PRs by these GitHub logins (case-insensitive). Bypassed by `include_prs`. | `[]` |
| `pr_sources.include_prs` *(session)* | Always include these PRs (by URL) | `[]` |
| `pr_sources.exclude_prs` *(session)* | Always exclude these PRs (by URL) | `[]` |
| `pr_sources.groups[].id` *(session)* | Group id; becomes feature id and branch name (`feature/<base>/<id>`) | — |
| `pr_sources.groups[].prs` *(session)* | Ordered list of PR URLs to cherry-pick onto a single branch and combine into one PR | — |
| `pr_sources.groups[].description` *(session)* | Title text for the combined PR | id |
| `pr_sources.groups[].if_exists` *(session)* | Override `pr_policy.if_exists` per group | inherits |
| `pr_sources.groups[].sort` *(session)* | Cherry-pick order within the group: `listed` (top-to-bottom of `prs:`) or `merged_at` (ascending GitHub merge timestamp; PR number breaks ties) | `listed` |
| `features[].id` *(session)* | Feature id; used as branch suffix `feature/<base>/<id>` | — |
| `features[].source_branch` *(session)* | Existing branch holding the feature's commits | — |
| `features[].description` *(session)* | Shown in the PR title and project board | — |
| `features[].enabled` *(session)* | Whether this feature is active on the next run | `true` |
| `features[].depends_on` *(session)* | Ordered list of feature ids that must be ported first | `[]` |

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
| `--session-file <path>` | Path to the session file. Overrides the `session_file:` key in config.yaml. | `<config-dir>/<name>.session.yaml` |
| `--version` | Print version and exit | — |

### At a glance: which command does what

The four "doing" commands look similar but operate on different things —
this matrix is the quickest way to pick the right one.

| | `run` | `continue` | `refresh` | `address-review` | `analyze-fails` |
|--|:-----:|:----------:|:---------:|:----------------:|:---------------:|
| Discovers new PRs from `pr_sources` (labels, include_prs, groups) | ✅ | — | — | — | — |
| Creates new port branches (cherry-pick onto `origin/<base>`) | ✅ | — | — | — | — |
| Opens new rebase PRs | ✅ for new ports | ✅ for branches that missed PR creation last time | — | — | — |
| AI-resolves **cherry-pick** conflicts (initial port) | ✅ | — | — | — | — |
| AI-resolves **merge** conflicts (target branch moved on) | — | — | ✅ | — | — |
| AI-addresses reviewer comments on a specific PR | — | — | — | ✅ | — |
| AI-investigates failing CI tests on a PR | — | — | — | — | ✅ |
| Iterates entries already in the project state file | only to skip / ensure-PR | ✅ all of them | ✅ all tracked PRs | — (stateless; acts on `--pr`) | ✅ all tracked PRs (or just `--pr`) |
| Mutates your local work-dir | ✅ (cherry-picks) | ✅ (push only) | ✅ (merges) | ✅ (appends commits) | ✅ (appends commits) |
| Pushes to origin | ✅ | ✅ | ✅ (only merge commits, on conflict-resolution) | ✅ (plain push, no force) | ✅ (plain push, no force) |

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
- **`analyze-fails`** — *"CI is red; ask the AI to triage and fix
  what it can."* Walks the failed CI statuses on a PR's head commit
  (or every tracked PR when `--pr` is omitted), filters down to the
  human-readable praktika reports (Fast test / Stateless tests /
  Integration tests), and invokes Claude **once per failed CI shard**
  with the full bundled failure list. Claude runs an iterative loop
  inside that one session: read every failure, group by likely root
  cause, fix the highest-leverage one, build, re-run **all** the
  still-failing tests in a single batch, see what's left, repeat.
  Many tests in CI fail for the same regression — this shape lets a
  single fix knock dozens of tests green at once instead of asking
  Claude to investigate each one independently. A "flaky-elsewhere"
  map cross-checks failure names against other tracked PRs and is
  fed into the prompt so master-side flakes get classified as
  `UNRELATED` instead of fix-attempts. Plain push at the end.
- **`address-review`** — *"a reviewer left comments; ask the AI to
  apply them."* Stateless, operates on `--pr <URL>`. Fetches every
  comment, filters down to those authored by trusted reviewers (the
  allowlist lives in `review_response.trusted_reviewers` and/or
  `--reviewer <login>`), then invokes the AI to append new commits
  addressing the feedback. **Linear history only** — no amend, no
  rebase; to retract something, the AI uses `git revert`. Plain
  `git push` at the end; a race with the PR author aborts without
  clobbering.

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
and (when `push: true` and `pr_policy.auto_pr: true`) opens a PR per port
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
            [--retry-failed | --no-retry-failed]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Version label used to derive `<project>-<version>` if `target_branch` is unset. Just a string — never resolved as a git ref. | from `target_branch` |
| `--work-dir <path>` | Working directory for git operations (overrides config `work_dir`). | from config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Toggle the AI resolver. The flag is a kill-switch: AI runs only if both this *and* `ai_resolve.enabled` are true. | on |
| `--retry-failed` / `--no-retry-failed` | Re-attempt PR units whose previous run ended in `conflict` status: discard the existing local / remote port branch and re-run the cherry-pick from base. With `--no-retry-failed`, those entries are left exactly as-is. | from `pr_policy.retry_failed` (true) |

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

#### `releasy analyze-fails` — investigate red CI on a PR (or every tracked PR)

Walks the **commit-status** entries on the PR's head SHA whose
`target_url` points at the praktika JSON viewer (typically
`https://altinity-build-artifacts.s3.amazonaws.com/json.html?…`),
classifies them as Fast test / Stateless / Integration, fetches the
underlying `result_*.json` artefact for each failed shard, and bundles
**all the failures from one shard** into a single Claude invocation.
GitHub-Actions job logs (e.g. `PR / Fast test (pull_request)`,
`RegressionTestsRelease / Iceberg (1) / iceberg_1`) are deliberately
ignored — only the parsed praktika reports drive per-test analysis.

The shape per shard is **iterative**, not one-shot:

1. Claude triages every failure in the shard, separating likely-related
   failures from flake / pre-existing issues.
2. Picks the highest-leverage root cause (the one fix that plausibly
   resolves the most tests).
3. Makes the smallest possible change, builds.
4. Re-runs every still-failing test from the shard in **one batch**
   (e.g. `tests/clickhouse-test t1 t2 t3 …` rather than once per test).
5. Reads what passes now, what's still failing.
6. Repeats steps 2–5 with the shrinking failure set, up to
   `max_iterations` build attempts per shard.

The full failed-test list for the shard is dropped on disk at
`.releasy/failed-tests.txt` so Claude has a reliable handle on it
even when the count exceeds what the prompt can inline.

By default the command also builds a **flaky-elsewhere map**: every
tracked PR's failed-test list is cross-referenced, and any test failing
on `flaky_elsewhere_threshold` (default 2) other tracked PRs is
flagged in the prompt as a likely master-side flake.

Per-shard outcomes:

- `DONE` — every test in the shard's input list now passes locally
  (or is confirmed `[unrelated]` flake).
- `PARTIAL` — at least one test was fixed; some are still failing or
  weren't investigated within budget. The common case for tricky
  shards.
- `UNRELATED` — the entire shard is master-side flake; no code
  changes.
- `UNRESOLVED` — Claude couldn't make progress (build broken, every
  attempt regressed).

The Anthropic spend for each PR is added to the matching feature's
`ai_cost_usd` (same field cherry-pick conflict resolution and
`refresh` write to), so the GitHub Project board's **AI Cost** column
shows the cumulative spend across every Claude-driven workflow on
that port — `releasy run` / `releasy refresh` / `releasy
analyze-fails` all roll up to one number per feature. The board sync
runs automatically at the end of an `analyze-fails` invocation when
`push: true`; if it's off, the cost still lands in `state.yaml` and
the next `releasy sync-project` (or any other syncing command) picks
it up.

```bash
releasy analyze-fails [--pr <URL>]
                      [--work-dir <path>]
                      [--dry-run]
                      [--push | --no-push]
                      [--no-flaky-check]
                      [--stateless ...]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <URL>` | PR on the configured origin. Omit to iterate every tracked PR with a `rebase_pr_url` in state. | — |
| `--work-dir <path>` | Working directory for git operations. | from config / cwd |
| `--dry-run` | List failed tests + flaky-elsewhere counts and exit. No Claude, no push. | off |
| `--push` / `--no-push` | Push the AI's commits to each PR's head branch. Plain push, no force; a race aborts. | on |
| `--no-flaky-check` | Skip the flaky-elsewhere assessment (don't fetch reports for other tracked PRs). | off |
| `--post-comment` / `--no-post-comment` | Post a top-level summary comment on each processed PR. The comment carries the per-shard classifications (`DONE` / `PARTIAL` / `UNRELATED` / `UNRESOLVED`), Claude's narration in a `<details>` block, and an explicit "Commits added: N (pushed / NOT pushed / —)" line so the operator can tell at a glance what actually changed. | `analyze_fails.post_comment_to_pr` (`true`) |
| `--stateless` | Skip session/state/lock and run against `--pr` alone. config.yaml is still loaded if present so AI settings, origin, and the build command are inherited. | off |
| `--origin <URL>` *(stateless only)* | Override origin remote URL. | config / derived from `--pr` |
| `--build-command <cmd>` *(stateless only)* | Shell command Claude runs to build before reproducing the failure. | from `ai_resolve.build_command` |
| `--claude-command <path>` *(stateless only)* | Executable used to invoke Claude. | from `analyze_fails.command` / `claude` |
| `--prompt-file <path>` *(stateless only)* | Prompt template. | bundled `prompts/analyze_fails.md` |
| `--timeout <seconds>` *(stateless only)* | Per-invocation Claude timeout. | from config / `7200` |
| `--max-iterations <n>` *(stateless only)* | Build-attempt cap per failed test. | from config / `6` |
| `--max-prs <n>` *(stateless only)* | Cap on tracked PRs to iterate when `--pr` is omitted (0 = no cap). | from config / `0` |

Config block (all optional):

```yaml
analyze_fails:
  command: claude                       # Claude executable
  prompt_file: prompts/analyze_fails.md # path resolved against config dir
  timeout_seconds: 7200
  max_iterations: 6
  max_prs_per_run: 0                    # 0 = no cap
  flaky_elsewhere_threshold: 2          # 0 = disable heuristic
  flaky_check_prs: 12                   # cap on cross-check PRs
  post_comment_to_pr: true              # post a summary comment per run

  # Optional: extend the Claude allowlist for the test runners /
  # binaries Claude needs to invoke. Use {work_dir} (alias: {repo_dir},
  # {cwd}) where you'd otherwise have to hardcode your absolute repo
  # path; RelEasy substitutes the live work-dir at invocation time.
  # allowed_tools:
  #   - Read
  #   - Edit
  #   - Write
  #   - Glob
  #   - Grep
  #   - Bash(git:*)
  #   - Bash(gh:*)
  #   - Bash(cd:*)
  #   - Bash(bash:*)
  #   - Bash(rm:*)
  #   - Bash(ls:*)
  #   - Bash(cat:*)
  #   - Bash(head:*)
  #   - Bash(tail:*)
  #   - Bash(tee:*)
  #   - Bash(rg:*)
  #   - Bash(ninja:*)
  #   - Bash(cmake:*)
  #   - Bash(make:*)
  #   - Bash(tests/clickhouse-test:*)
  #   - Bash(tests/integration/runner:*)
  #   - Bash(pytest:*)
  #   - Bash({work_dir}/build/programs/clickhouse:*)
```

Exit code: `1` on any per-PR failure (couldn't fetch metadata, push
race, non-linear history, …); `0` otherwise even if every test was
classified `UNRELATED`.

> **Linear history only**, same rules as `address-review`: Claude may
> only append new commits — no amend, no rebase, no `reset --hard`,
> no force-push. Retracting a previous change happens via
> `git revert <sha>`.

#### `releasy address-review` — AI-address reviewer comments

Fetches every comment on a PR (issue comments, inline review comments,
review bodies), filters them down to **comments authored by trusted
reviewers**, and asks Claude to append code changes addressing the
feedback. Fully stateless — the PR does not need to be tracked in
`state.yaml`.

Injection-safety: untrusted comments are dropped at fetch time,
*before* the prompt is rendered, so a hostile commenter cannot smuggle
instructions into the run. The allowlist is explicit: if both
`review_response.trusted_reviewers` and `--reviewer` are empty, the
command refuses to run.

The two sources **add together** — an explicit `--reviewer <login>`
on the CLI is authoritative on its own and does NOT need to appear in
the config allowlist first. That's the whole point of the flag:
process one reviewer's feedback ad-hoc without editing `config.yaml`.

**Stateful re-runs.** When `--pr` matches a rebase PR RelEasy already
tracks in state (i.e. one it opened via `releasy run`), a successful
`address-review` run stamps `last_review_addressed_at` onto that
feature. The next run on the same PR uses it as an implicit (exclusive)
`--since` default — so you can rerun with no arguments to pick up just
the newest round of feedback. Stateless PRs (anything RelEasy didn't
open) skip this entirely; pass `--since` explicitly when you need
incremental behaviour there.

**Linear history only:** the AI may only append new commits. No amend,
no rebase, no `reset --hard`, no force-push. To retract a previous
change, it uses `git revert <sha>` (which creates a new forward
commit).

**What the AI does per comment:**

- **Actionable** → edits code + commits. The commit message references
  the comment URL so the reviewer can trace it back.
- **Non-actionable** (already fixed, out of scope, misunderstanding) →
  posts a short in-thread reply with a `🤖 *Generated by releasy
  address-review*` footer. Inline comments reply threaded (same
  conversation); issue comments and review bodies reply top-level
  (GitHub has no thread model for those).
- **Truly out of scope** (e.g. "refactor this whole module") → AI
  declines, surfaces in the terminal narration, human decides.

```bash
releasy address-review --pr <URL>
                       [--reviewer <login>]...
                       [--since <ISO8601>]
                       [--work-dir <path>]
                       [--dry-run]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <URL>` (required) | PR on the configured origin. Head branch must also live on origin (no fork PRs in v1). | — |
| `--reviewer <login>` | GitHub login (repeatable). Adds to `review_response.trusted_reviewers`; authoritative on its own (does not have to be in the config list first). Case-insensitive. | `[]` |
| `--since <URL\|ISO8601>` | Only consider comments newer than this reference. Two forms: a GitHub comment URL (`…#issuecomment-<id>` / `#discussion_r<id>` / `#pullrequestreview-<id>`) → everything **strictly after** that comment; or an ISO-8601 timestamp → comments at or after that moment. Omit to consider every comment (or, in stateful mode, everything after the previous `address-review` run on this PR). | auto from state; else every comment |
| `--reply` / `--no-reply` | Post an in-thread reply (with a bot footer) on every comment the AI classifies as non-actionable. Overrides `review_response.reply_to_non_addressable`. | on |
| `--stateless` | Skip the session and state files (no project lock, no ownership check, no state I/O). `config.yaml` IS still loaded from the usual `--config` / cwd resolution and supplies AI settings, trusted_reviewers, etc. If no `config.yaml` is found, a synthetic config is built from CLI flags alone. | off |
| `--origin <URL>` | (stateless only) Override the origin remote URL from config (or derive it from `--pr` when no config). Use when you need ssh instead of https. | config value, or derived from `--pr` |
| `--build-command <cmd>` | (stateless only) Shell command the AI may run to verify changes compile. Overrides `ai_resolve.build_command` in config. Empty ⇒ no build. | config value |
| `--claude-command <path>` | (stateless only) Executable used to invoke Claude. Overrides `review_response.command` in config. | config value / `claude` |
| `--prompt-file <path>` | (stateless only) Prompt template path. Overrides `review_response.prompt_file`. | config value / bundled |
| `--timeout <seconds>` | (stateless only) Per-invocation Claude timeout. Overrides `review_response.timeout_seconds`. | config value / `7200` |
| `--max-iterations <n>` | (stateless only) Build-attempt cap. Overrides `review_response.max_iterations`. | config value / `15` |
| `--post-summary-comment` | (stateless only) Also post one top-level summary comment on the PR. | off |
| `--work-dir <path>` | Working directory for git operations. | from config / cwd |
| `--dry-run` | Fetch + print the filtered comment list, then exit. No AI, no push. | off |

For every comment the AI classifies as non-actionable (already fixed,
out of scope, or a misunderstanding), it posts a short reply directly
in-thread with a machine-readable bot footer — reviewers know at a
glance which answers came from Claude. ADDRESSABLE comments are
answered by the commit that fixes them (the commit message references
the comment URL). Pass `--no-reply` for a silent run that only reports
via the AI's terminal narration.

**`--stateless` mode** — for ad-hoc runs that should skip the session
and state files (e.g. addressing a reviewer's feedback without taking
the project lock or recording a timestamp):

```bash
# Inside a project dir — config.yaml is still picked up for AI settings
# and trusted reviewers; CLI flags override where you need them.
cd ~/work/antalya-26.3
releasy address-review --stateless \
  --pr https://github.com/Altinity/ClickHouse/pull/1687 \
  --reviewer ianton-ru

# With no project config available — everything comes from CLI flags.
releasy address-review --stateless \
  --pr https://github.com/Altinity/ClickHouse/pull/1687 \
  --reviewer ianton-ru \
  --work-dir ~/work/ClickHouse \
  --build-command "cd build && ninja"
```

`config.yaml` is loaded when present (use `--config <path>` to pick a
specific one); the session and state files are always skipped. The
stateless-only override flags (`--origin`, `--build-command`,
`--claude-command`, `--prompt-file`, `--timeout`, `--max-iterations`,
`--post-summary-comment`) are rejected without `--stateless` so the
intent stays unambiguous.

Config (entirely optional — the command works with just
`--reviewer <login>` on the CLI):

```yaml
review_response:
  trusted_reviewers:        # GitHub logins — case-insensitive
    - alice
    - bob
  # Flip to false to skip per-comment replies (list declined comments
  # in the AI's stdout narration only). Default: true.
  reply_to_non_addressable: true
  # Optional: post one extra summary comment at the end describing
  # everything that was done. Default: false.
  post_summary_comment: false
```

Exit code: `1` on any failure (missing allowlist, AI failed, push
race, non-linear history detected, …); `0` on success or when there
were no trusted comments to address.

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

- **Incompatible with session `pr_sources.groups`** — session load fails with a
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

`releasy new` writes TWO files side-by-side: `config.yaml` at `--out`
and `<name>.session.yaml` in the same directory. Refuses to overwrite
either if one already exists.

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

`features:` in the session file lists static port targets — branches that
already exist somewhere and need to be re-applied onto each base. PR
sources (`pr_sources.*`, also in the session file) are the dynamic
counterpart; this group manages the static list.

```bash
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

| Subcommand | Description |
|------------|-------------|
| `feature add` | Append a new entry to `features:` in the session file. Requires `--id`, `--source-branch`, `--description`. |
| `feature enable` | Set `enabled: true` on a feature (runs participate in `releasy run`). Requires `--id`. |
| `feature disable` | Set `enabled: false` (skipped by `releasy run`). Requires `--id`. |
| `feature remove` | Delete the feature from the session file. Doesn't touch any branches. Requires `--id`. |
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
> the branch was pushed but no PR was opened (`pr_policy.auto_pr:
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

- `config.yaml` — stable per-project configuration (origin, target
  branch, AI settings, notifications, `pr_policy`). The user provides
  this; `releasy new` scaffolds it.
- `<config-dir>/<name>.session.yaml` — per-effort source data:
  `features:` list, `pr_sources:` selectors. Scaffolded alongside
  `config.yaml` by `releasy new`; mutated by `releasy feature *`
  subcommands. Override its path with `session_file:` in config.yaml
  or `--session-file` on the CLI.
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

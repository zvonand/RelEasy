# RelEasy — Specification (v1)

> **Outdated.** This document describes the original single-project,
> in-repo-state design. RelEasy now supports multiple concurrent
> projects per machine: each `config.yaml` carries a unique `name:`,
> pipeline state lives outside the repo under
> `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml`, there
> is no `STATUS.md`, and a per-project lock keeps concurrent runs of
> the same project serialized.
>
> See [README.md](README.md) for the current behaviour, including the
> `releasy new` / `releasy list` / `releasy where` / `releasy adopt`
> commands. The branch-naming, pipeline-phase, and PR-discovery design
> below is still accurate; the "Repository Layout" and "State" sections
> are not.

## 1. Overview

A standalone CLI tool that ports features and PRs onto a stable **base branch** on origin. Rather than rebasing long-lived branches in place, each feature / PR is cherry-picked onto its own port branch (`feature/<base>/<id>`) and optionally opened as a PR. RelEasy tracks a single repo (`origin`); PRs from other repos can still be ported by listing their full URLs in `pr_sources.include_prs` / `pr_sources.groups[].prs`.

The pipeline is **resumable**: state is persisted between runs, and each `releasy run` picks up from where the previous run stopped.

---

## 2. Repository Layout

```
releasy/
├── config.yaml        ← origin URL, features, PR sources (gitignored)
├── config.yaml.example ← documented template
├── state.yaml         ← persisted pipeline state (auto-managed)
├── STATUS.md          ← human-readable branch status table (auto-managed)
└── src/releasy/       ← tool source code
```

---

## 3. Branch Naming Convention

The base/target branch is taken from `target_branch` in config when set;
otherwise it's derived as `<project>-<version>` where `<version>` is parsed
from the `--onto` value:
- `26.3` → `26.3`
- Tag-style strings like `v26.3.4.234-lts` → `26.3`
- Raw SHAs → first 8 characters

`--onto` is a **naming label**, not a git ref. RelEasy never tries to
fetch or resolve it; the base branch itself must already exist on origin.

| Type | Pattern | Example |
|------|---------|---------|
| Base | `target_branch` or `<project>-<version>` | `antalya-26.3` |
| Feature | `feature/<base>/<id>` | `feature/antalya-26.3/s3-disk` |
| Origin PR feature | `feature/<base>/pr-<N>` | `feature/antalya-26.3/pr-42` |
| External PR feature | `feature/<base>/<owner>-<repo>-pr-<N>` | `feature/antalya-26.3/ClickHouse-ClickHouse-pr-12345` |

- `<project>` is taken from the top-level `project:` config key
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

# Project identifier — used for derived branch names
project: antalya

# Target/base branch PRs are opened into.
# When set, --onto becomes optional on the CLI.
# When unset, the base branch is derived as <project>-<version-from-onto>.
target_branch: antalya-26.3

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
  # auto_pr: true   # default; open PR for every pushed port branch
  by_labels:
    - labels: ["forward-port", "v26.3"]
      description: "Forward-ported changes"
      merged_only: true
      if_exists: skip

  exclude_labels: ["do-not-port"]

  include_prs:
    - https://github.com/Altinity/ClickHouse/pull/123

  exclude_prs:
    - https://github.com/Altinity/ClickHouse/pull/789

  # Sequential PR groups: cherry-pick multiple PRs onto ONE branch in
  # listed order and open ONE combined PR. Use when later PRs depend on
  # earlier ones.
  groups:
    - id: iceberg-rest
      description: "Iceberg REST catalog support"
      prs:
        - https://github.com/Altinity/ClickHouse/pull/1500
        - https://github.com/Altinity/ClickHouse/pull/1512
        - https://github.com/Altinity/ClickHouse/pull/1530

notifications:
  github_project: https://github.com/orgs/Altinity/projects/1
```

**Notes:**
- `push: false` (default) keeps everything local — no branches pushed, no PRs created, no project board sync.
- `work_dir` can point to an existing git clone. If the directory has a `.git`, it's used directly (no clone).
- `origin` is required — the repo where branches are pushed and PRs are created.
- `pr_sources.*.if_exists`: `skip` (default) leaves existing port branches alone; `recreate` rebuilds them from scratch.
- `pr_sources.by_labels[].labels`: AND logic — a PR must have ALL listed labels. Multiple `by_labels` entries are unioned.
- `pr_sources.exclude_labels`: any PR carrying at least one of these labels is dropped (unless it appears in `include_prs`).
- `pr_sources.include_prs`: always include these PRs by URL, regardless of labels. URLs may point to any public GitHub repo — cross-repo PRs are fetched directly from the URL (no extra git remote is added). Their port branches are named `feature/<base>/<owner>-<repo>-pr-<N>` so they cannot collide with same-numbered origin PRs.
- `pr_sources.exclude_prs`: always exclude these PRs by URL — final override. Matched by the full `(owner, repo, number)` triple.
- `pr_sources.auto_pr`: global switch (default `true`). When `true` and `push: true`, every pushed port branch — singleton, by_labels, include_prs, or group — gets a PR opened against the base. Set `false` to push branches only and open PRs manually.
- `pr_sources.groups`: each group is one *unit of work* — its PRs are cherry-picked, in listed order, onto a single port branch named `feature/<base>/<id>`, and (when `pr_sources.auto_pr` and `push`) result in ONE combined PR. A PR may appear in at most one group; PRs claimed by a group are removed from the standalone stream. `exclude_prs` and `exclude_labels` still drop individual PRs from a group; an emptied group is skipped with a warning. AI conflict resolution runs per cherry-pick step (resolve + build + commit, locally only); RelEasy pushes the branch and opens the combined PR after all steps succeed and tags the PR with `ai_resolve.label` if any step needed AI.
- PRs are processed in merge order (earliest merged first); groups sort by their earliest constituent merge time.
- `enabled: false` excludes a feature from the pipeline entirely.
- `depends_on` declares inter-feature dependencies (manual ordering in v1).

---

## 5. Authentication

- **Git operations** (clone, fetch, push): SSH key via agent or `RELEASY_SSH_KEY_PATH`.
- **GitHub API** (PR discovery, PR creation, project board): `RELEASY_GITHUB_TOKEN` with `repo` and `project` scopes.

---

## 6. Pipeline

### Invocation

```
releasy run [--onto <version-label>]
```

`--onto` is optional when `target_branch` is set in config; otherwise it
supplies the version suffix used to derive the base branch name (see
§3 Branch Naming Convention). The value is a label only — it is never
resolved as a git ref. The base branch itself must already exist on
origin; the pipeline verifies this and then ports each PR / feature onto
it. State is saved after each port so the run is resumable.

---

### Port PRs onto Base

#### PR-based features

PRs are collected and filtered using set arithmetic:

1. **Collect:** For each `by_labels` entry, search GitHub for PRs matching all labels (Issues API, AND logic). Union the results.
2. **Exclude by label:** Remove any PR carrying at least one `exclude_labels` label (unless it's in `include_prs`).
3. **Include individual PRs:** Fetch any `include_prs` URLs not already in the set.
4. **Exclude individual PRs:** Remove any `exclude_prs` URLs — final override.
5. **Materialise groups:** For each `pr_sources.groups[]`, fetch its PRs (subject to `exclude_prs` / `exclude_labels`). PRs claimed by a group are removed from the singleton stream and processed as one feature unit.

A *feature unit* is either a single PR or a sequential PR group. For each unit (sorted by earliest merge time):

1. Create feature branch `feature/<project>-<version>/<id>` from base. The id is `pr-<N>` for singletons or the group `id` for groups.
2. Cherry-pick each PR's merge commit (`-m 1`) in listed order.
3. On a clean run: push and (if `push` + `pr_sources.auto_pr`) open ONE PR. Group PRs include a body listing every constituent PR.
4. On conflict (singleton or any step inside a group): try AI resolve in *step mode* — Claude resolves the conflict, builds, and commits locally; RelEasy then continues with the next cherry-pick (or finalises the unit). On AI failure (resolver disabled, or it gave up after `max_iterations` build attempts): clean up and flag the unit. Singleton / first-of-group: abort the cherry-pick, drop the local port branch, no push, no PR — entry marked `Conflict` in the GitHub Project. Partial group (later step failed, n-1 prior picks valid): abort the failed pick only, push the branch, open a DRAFT PR labelled `ai-needs-attention` with a banner explaining what failed; remaining PRs in the group are skipped, entry marked `Conflict`. Either way the pipeline keeps moving with the next unit.
5. Each cherry-pick produces ONE commit (`git cherry-pick -m 1 --no-edit`; AI build-fix iterations only `--amend` that same commit). For groups, RelEasy appends a `Source-PR: #<N> (<url>)` trailer to each commit so the combined PR's commit list is self-attributing.
6. After all PRs in a unit are applied (clean or AI-resolved), push the branch and (when `pr_sources.auto_pr` + `push`) open ONE PR. If any step used AI, the PR title gets the `ai-resolved:` prefix and the PR is tagged with `ai_resolve.label`.

After all units: `phase = ports_done`.

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

## 8. PR title format & identification

Rebase PR titles are `"<Project> <version>: <subject>"` (e.g.
`"Antalya 26.3: Token Authentication and Authorization"`). The version is
extracted from the base branch (`<project>-<version>`; falls back to the
whole branch name for custom `target_branch` values). The project name
is title-cased when it's all lowercase, otherwise preserved verbatim.

The source PR's title is first run through a sanitiser that strips a
leading `<version>[<project>]:` prefix some workflows embed (e.g.
`"26.1 Antalya: …"`), so the rebase PR title accurately reflects its real
target rather than the source's original target.

Identification is done via labels, not text in the title:

- Every PR opened or updated by RelEasy is tagged with the `releasy`
  label (auto-created with colour `#1F6FEB` on first run when
  `push: true`).
- AI-resolved PRs additionally get `ai_resolve.label` (default
  `ai-resolved`).

---

## 9. Safety: writes always target origin

RelEasy operates as a **read-from-anywhere, write-only-to-origin** tool:

- **Reads** from any public GitHub repo are allowed when a cross-repo PR URL is supplied via `pr_sources.include_prs` / `pr_sources.groups[].prs`. These are fetched directly by URL — the source repo is never added as a git remote.
- **Writes** (PR creation, PR title/body edits, label add, branch push) only ever target the configured `origin`. The slug is re-derived on every write call and passed through `_assert_writes_target_origin`, which raises `ValueError` if the resolved target ever differs from origin. The branch-push helper `force_push(repo_path, branch, config)` takes the `Config` itself and pushes to `config.origin.remote_name` — there is no parameter to point it at a different remote.

Each `releasy run` prints `PRs will be opened against <owner>/<repo> (origin)` immediately after resolving the base branch, so the write target is visible in the run log.

---

## 10. State (`state.yaml`)

Auto-updated after every operation.

```yaml
last_run:
  started_at: 2026-03-21T10:00:00Z
  onto: v26.3.4.234-lts
  phase: ports_done          # init | ports_done
  base_branch: antalya-26.3
  features:
    pr-42:
      status: needs_review
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

## 11. Status Table (`STATUS.md`)

Updated after every state change.

```markdown
## RelEasy Branch Status

Last run: 2026-03-21 · Onto: `v26.3.4.234-lts` · Phase: ports_done

| Branch | Status | Based On | PR | Conflict Files |
|--------|--------|----------|----|----------------|
| feature/antalya-26.3/pr-42 | 🔵 needs-review | `v26.3.4.234-` | #42 | |
| feature/antalya-26.3/s3-disk | 🔴 conflict | `v26.3.4.234-` | | `src/Storages/StorageS3.cpp` |
```

---

## 12. CLI Reference

```
# Pipeline
releasy [--config <path>] run [--onto <tag-or-sha>] [--work-dir <path>]
releasy continue --branch <name>
releasy skip --branch <name>
releasy abort
releasy status

# Release
releasy release --base-tag <tag> --name <branch-name> [--strict] [--include-skipped]

# Feature management
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

---

## 13. GitHub Project Integration

Sync branch status to a GitHub Projects v2 board (optional). One-time manual setup, then RelEasy keeps it updated automatically.

### Manual Setup

1. **Create the project:** GitHub org → Projects → New project (Table layout recommended).
2. **Configure the Status field:** Edit the default "Status" single-select field. Set options to exactly: `Needs Review`, `Branch Created`, `Conflict`, `Skipped` (case-insensitive match).
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
- Creates one draft-issue card per port branch.
- Sets the Status field to the pipeline state (Needs Review, Conflict, etc.).
- Updates the card body with the base commit and conflicted files.
- On re-runs, deletes old cards and recreates with updated status.

No cards or views need to be created manually. Project sync is skipped when `push: false`.

---

## 14. Non-Goals (v1)

- No automatic base-ref discovery (`--onto` is always explicit).
- No AI-assisted conflict resolution.
- No automatic dependency-aware ordering (`depends_on` declared but ordering is manual).
- No build/test validation after rebase.

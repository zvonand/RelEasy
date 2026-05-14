# Command reference

`releasy <command> --help` is authoritative. This page is the quick map.

For the model behind these commands, see [concepts.md](concepts.md). For
config they read, see [configuration.md](configuration.md).

## Contents

- [Global options](#global-options)
- [At a glance: which command does what](#at-a-glance-which-command-does-what)
- Pipeline:
  [`run`](#releasy-run) ·
  [`refresh`](#releasy-refresh) ·
  [`discover-deps`](#releasy-discover-deps) ·
  [`analyze-fails`](#releasy-analyze-fails) ·
  [`address-review`](#releasy-address-review) ·
  [`continue`](#releasy-continue) ·
  [Sequential mode](#sequential-mode) ·
  [`skip`](#releasy-skip) ·
  [`abort`](#releasy-abort)
- Inspection: [`status`](#releasy-status)
- Multi-project:
  [`new`](#releasy-new) ·
  [`list`](#releasy-list) ·
  [`where`](#releasy-where) ·
  [`adopt`](#releasy-adopt)
- Project board:
  [`setup-project`](#releasy-setup-project) ·
  [`sync-project`](#releasy-sync-project)
- Release: [`release`](#releasy-release)
- Features: [`feature *`](#feature-management)

## Global options

| Option | Description | Default |
|--------|-------------|---------|
| `--config <path>` | Path to `config.yaml` | `./config.yaml` |
| `--session-file <path>` | Path to session file. Overrides `session_file:` in config. | `<config-dir>/<name>.session.yaml` |
| `--version` | Print version and exit | — |

## At a glance: which command does what

| | `run` | `continue` | `refresh` | `address-review` | `analyze-fails` |
|--|:-----:|:----------:|:---------:|:----------------:|:---------------:|
| Discovers new PRs | ✅ | — | — | — | — |
| Creates new port branches | ✅ | — | — | — | — |
| Opens new rebase PRs | ✅ new | ✅ missed | — | — | — |
| AI-resolves cherry-pick conflicts | ✅ | — | — | — | — |
| AI-resolves merge conflicts (target moved on) | — | — | ✅ | — | — |
| AI-addresses reviewer comments | — | — | — | ✅ | — |
| AI-investigates failing CI | — | — | — | — | ✅ |
| Iterates state entries | only skip / ensure-PR | ✅ all | ✅ all tracked | — (stateless on `--pr`) | ✅ all tracked |
| Mutates work-dir | ✅ cherry-picks | ✅ push only | ✅ merges | ✅ commits | ✅ commits |
| Pushes to origin | ✅ | ✅ | ✅ (merges only) | ✅ (plain) | ✅ (plain) |

One-liners:

- **`run`** — *do new work.* Discover, cherry-pick, push, open PRs.
- **`continue`** — *I fixed something by hand; reconcile state.* Push/open
  what's pending. No git ops beyond push + status checks.
- **`refresh`** — *keep open PRs current with the moved-on base.* Merge
  target in, AI-resolve conflicts.
- **`analyze-fails`** — *CI is red; let AI triage.* Iterative per-shard
  fix loop.
- **`address-review`** — *reviewer left comments; let AI apply them.*
  Stateless, linear history only.

> **Why both `run` and `continue`?** `run` only acts on PRs it's
> cherry-picking right now. If you fix a conflict by hand on a branch
> with **no rebase PR yet**, `run` either skips it (`if_exists: skip`)
> or rebuilds from base (`recreate`). `continue` preserves your manual
> fix and just pushes + opens the PR.

[`discover-deps`](#releasy-discover-deps) is a read-only diagnostic
sibling of `run` — see its section.

The rest ([`skip`](#releasy-skip), [`abort`](#releasy-abort),
[`status`](#releasy-status), board-sync, release, feature) never touch git
history.

## Pipeline

### `releasy run`

*Port PRs onto the base branch.*

Discovers PRs from `pr_sources`, creates port branches from
`origin/<base>`, cherry-picks, opens PRs. AI-resolves cherry-pick
conflicts when `ai_resolve.enabled` is on. Unresolved → singleton dropped
or partial-group draft PR with `ai-needs-attention`. See
[Conflict resolution](concepts.md#conflict-resolution).

For PRs with an existing rebase PR, `run` doesn't rebuild — it routes
through the same merge-target flow [`refresh`](#releasy-refresh) uses:
clean merge → leave alone; conflict → AI-resolve and plain push (never
force). `if_exists: append` is the only setting that cherry-picks new
commits on top of an existing PR.

```bash
releasy run [--onto <ver>] [--work-dir <path>]
            [--resolve-conflicts | --no-resolve-conflicts]
            [--retry-failed | --no-retry-failed]
            [--merge-target | --no-merge-target]
            [--only <url-or-id>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--onto <ver>` | Version label for derived base name. Naming-only — never resolved as a git ref. | from `target_branch` |
| `--work-dir <path>` | Working dir for git ops. | config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Kill-switch for AI resolver. AI runs only if both this and `ai_resolve.enabled` are true. | on |
| `--retry-failed` / `--no-retry-failed` | Re-attempt entries in `conflict` status. No-PR-yet: rebuild from base (only when `if_exists: recreate`). PR open: merge-target flow (PR always preserved). | `pr_policy.retry_failed` |
| `--merge-target` / `--no-merge-target` | Push a merge commit on PRs even without conflicts. Never force-pushes. | off |
| `--only <url-or-id>` | Single PR URL **or** group/singleton id. Drops everything else. Non-zero if nothing matches. | — |

Exit: `1` on any `conflict`, else `0`.

### `releasy refresh`

*Keep tracked PRs current with the target branch.*

Maintenance pass — **never opens PRs, never discovers, never
cherry-picks**. For each tracked PR with a rebase PR URL, attempts
`git merge --no-ff origin/<base>`:

- **clean** → leave PR alone (or push a merge commit with `--merge-target`)
- **conflict + AI resolves** → push, restore status, set `ai_resolved`
- **conflict + AI gives up** → reset local, mark `conflict`

Uses `ai_resolve.merge_prompt_file`. Suitable for cron. Note that
[`run`](#releasy-run) applies the same flow to PRs with `--merge-target`
— explicit `refresh` is mainly for cron cadence.

```bash
releasy refresh [--work-dir <path>]
                [--resolve-conflicts | --no-resolve-conflicts]
                [--merge-target | --no-merge-target]
                [--only <url-or-id>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--work-dir <path>` | Working dir. | config / cwd |
| `--resolve-conflicts` / `--no-resolve-conflicts` | Toggle AI resolver. | on |
| `--merge-target` / `--no-merge-target` | Push a merge commit even on clean merges. Never force-pushes. | off |
| `--only <url-or-id>` | Single tracked PR (URL — source or rebase) or feature/group id. | — |

Exit: `1` on any `conflict`, else `0`.

### `releasy discover-deps`

*Auto-discover a PR dependency DAG.*

Trial-cherry-picks every candidate in a scratch worktree, traces conflicts
to older un-ported PRs touching the same files, emits a YAML grouping +
writes a deps overlay at `<session-stem>.deps.yaml` that the loader picks
up on the next [`run`](#releasy-run). Main session is never modified.

Declared `pr_sources.groups[]` are treated as **single super-nodes** —
discovery never subdivides them.

```bash
releasy discover-deps [--onto <ver>] [--work-dir <path>]
                      [-o <path>] [--deps-file <path> | --no-write]
                      [--no-ai] [--max-depth <N>] [--limit <N>]
                      [--include-already-merged]
```

| Output | Where | Override |
|--------|-------|----------|
| Diagnostic report (always written) | `<config-dir>/discover-deps.<base>.yaml` | `-o <path>` |
| Deps overlay (consumed by `run`) | `<session-stem>.deps.yaml` | `pr_sources.deps_file:` in session, or `--deps-file <path>`, or `--no-write` to skip |

`--no-write` and `--deps-file` are mutually exclusive.

**Hybrid AI flow per conflict:**

1. **Deterministic** — `git log target..source -- <file>` →
   `Source-PR:` trailers + merge-containment → candidate unit IDs.
2. **Candidates found** → ask Claude (text-only, no tools) to
   confirm/refine. `discovery_method: git-graph+claude`.
3. **No candidates** → invoke full AI resolver (tools, builds). Outcomes:
   `MISSING_PREREQS:` → those become deps (`ai-resolve`); resolver
   succeeds → no deps needed (`ai-resolve-clean`); resolver fails →
   empty deps + warning (`git-graph`).
4. **Always reset** the scratch worktree.

`--no-ai` skips both AI steps. Trade-off: fast/free but the deterministic
mapping misses semantic dependencies.

**Port-branch caching:** when overlay write is enabled, the trial-pick
result is preserved as `feature/<base>/<unit_id>`. The next
[`run`](#releasy-run) reuses it via `if_exists: skip` — no re-cherry-pick,
**no second AI resolve**. `--no-write` disables caching too (true
dry-run). Re-runs always rewrite cache branches.

**Round-trip notes:**

- Auto-discovered singletons become **1-PR groups** in the overlay
  (carries `depends_on:`). Branch naming and AI-context semantics shift
  to `is_group=True`. Move the entry into the main session (and drop
  `auto_discovered:`) to make permanent.
- Re-running rewrites the deps file from scratch. Hand-edits there will
  be lost; use `--no-write` or `--deps-file <path>` to redirect.
- Cycles in `depends_on` (from hand-edits) are rejected at session-load.

**After the target moves:** just re-run `discover-deps`. PRs that landed
upstream drop out automatically. Summary line:

```
discover-deps · base=antalya-26.3 · 24 candidates · 8 already in target
  refresh: 3 removed [auto-pr-100, ...] · 1 added [auto-pr-300]
```

Exit: `0` regardless of conflicts found — read-only diagnostic.

### `releasy analyze-fails`

*Investigate red CI on a PR (or every tracked PR).*

Walks failed commit-status entries on the PR's head SHA whose `target_url`
points at the praktika JSON viewer (GitHub-Actions job logs are
deliberately ignored). Per failed shard, bundles all failures into a
single Claude invocation that runs iteratively: triage → pick highest-
leverage root cause → fix → build → re-run still-failing tests in one
batch → repeat (up to `max_iterations`).

A **flaky-elsewhere map** cross-references failures across other tracked
PRs (`flaky_elsewhere_threshold` default 2) so master-side flakes get
classified `UNRELATED` instead of fix attempts.

Per-shard outcomes:

| Outcome | Meaning |
|---------|---------|
| `DONE` | Every test now passes (or confirmed flake). |
| `PARTIAL` | Some fixed; some still failing or unexplored. Common. |
| `UNRELATED` | Whole shard is master-side flake. No code changes. |
| `UNRESOLVED` | Couldn't make progress. |

The failed-test list lands at `.releasy/failed-tests.txt` for the AI to
read. Anthropic spend rolls into `ai_cost_usd` (same field as `run` /
`refresh`) and surfaces on the board's
[`AI Cost`](configuration.md#what-gets-synced) column.

```bash
releasy analyze-fails [--pr <URL>] [--work-dir <path>]
                      [--dry-run]
                      [--push | --no-push]
                      [--no-flaky-check]
                      [--post-comment | --no-post-comment]
                      [--only <url-or-id>]
                      [--stateless ...]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <URL>` | PR on origin. Omit to iterate every tracked PR with a `rebase_pr_url`. | — |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | List failed tests + flake counts, exit. No Claude, no push. | off |
| `--push` / `--no-push` | Push AI commits. Plain push, no force; race aborts. | on |
| `--no-flaky-check` | Skip flaky-elsewhere assessment. | off |
| `--post-comment` / `--no-post-comment` | Per-PR summary comment with outcomes + commit list. | `analyze_fails.post_comment_to_pr` |
| `--only <url-or-id>` | Single tracked PR / feature / group. Mutex with `--pr` and `--stateless`. | — |
| `--stateless` | Skip session/state/lock; act on `--pr` alone. `config.yaml` still loaded if present. | off |

Stateless-only overrides: `--origin`, `--build-command`, `--claude-command`,
`--prompt-file`, `--timeout`, `--max-iterations`, `--max-prs`.

Custom Claude allowlists for test runners go in `config.yaml`. Use
`{work_dir}` (alias `{repo_dir}`, `{cwd}`) so paths aren't hard-coded:

```yaml
analyze_fails:
  allowed_tools:
    - Read
    - Bash(git:*)
    - Bash(tests/clickhouse-test:*)
    - Bash({work_dir}/build/programs/clickhouse:*)
```

Exit: `1` on any per-PR failure (fetch / push race / non-linear history);
`0` otherwise even if everything is `UNRELATED`.

> **Linear history only** — same as
> [`address-review`](#releasy-address-review). Append commits only. To
> retract: `git revert <sha>`.

### `releasy address-review`

*Apply reviewer comments via AI.*

Fetches every comment on the PR, filters to trusted-reviewer logins, asks
Claude to append fixes. Fully stateless — the PR doesn't need to be in
state.yaml.

**Allowlist required.** Either `review_response.trusted_reviewers` or
`--reviewer <login>` (or both — they add). Both empty → command refuses.
The two sources add together; `--reviewer` is authoritative on its own.

**Injection-safe:** untrusted comments are dropped at fetch time, before
the prompt is built.

**Linear history only.** Append commits only — no amend, no rebase, no
`reset --hard`, no force-push. Retracting: `git revert <sha>`.

**Stateful re-runs:** when `--pr` matches a rebase PR RelEasy tracks, a
successful run stamps `last_review_addressed_at`; the next run uses it as
implicit (exclusive) `--since`. Stateless PRs: pass `--since` explicitly.

**Per comment, AI does:**

- **Actionable** → edit + commit. Message references the comment URL.
- **Non-actionable** (already fixed / OOS / misunderstanding) → in-thread
  reply with a `🤖 *Generated by releasy address-review*` footer.
- **Truly out of scope** → declines; surfaces in narration.

```bash
releasy address-review --pr <URL>
                       [--reviewer <login>]...
                       [--since <URL|ISO8601>]
                       [--reply | --no-reply]
                       [--work-dir <path>]
                       [--dry-run]
                       [--stateless ...]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--pr <URL>` (required) | PR on origin. Head branch must live on origin (no fork PRs). | — |
| `--reviewer <login>` (repeatable) | Login allowlist; adds to config. Case-insensitive. | `[]` |
| `--since <URL\|ISO8601>` | Comment URL → strictly after; ISO timestamp → at or after. | auto from state, else all |
| `--reply` / `--no-reply` | Post in-thread reply on non-actionable comments. | `review_response.reply_to_non_addressable` |
| `--work-dir <path>` | Working dir. | config / cwd |
| `--dry-run` | Fetch + print filtered comment list, exit. | off |
| `--stateless` | Skip session/state. `config.yaml` still loaded if present. Synthesizes config from CLI if absent. | off |

Stateless-only overrides: `--origin`, `--build-command`, `--claude-command`,
`--prompt-file`, `--timeout`, `--max-iterations`, `--post-summary-comment`.
Rejected without `--stateless`.

```bash
# Inside a project dir — config picks up AI settings + reviewers.
releasy address-review --stateless \
  --pr https://github.com/Altinity/ClickHouse/pull/1687 \
  --reviewer ianton-ru

# Pure CLI — no config at all:
releasy address-review --stateless \
  --pr https://github.com/Altinity/ClickHouse/pull/1687 \
  --reviewer ianton-ru \
  --work-dir ~/work/ClickHouse \
  --build-command "cd build && ninja"
```

Exit: `1` on any failure (missing allowlist, AI failed, push race,
non-linear history); `0` on success or when no trusted comments.

### `releasy continue`

*Reconcile state after a manual fix.*

Walks every port in state. Doesn't discover, doesn't cherry-pick, doesn't
merge. Per entry:

| State | Action |
|-------|--------|
| `skipped` | leave |
| `conflict`, AI gave up (`failed_step_index` set) | highlight; user must act |
| `conflict`, branch clean (manually resolved) | push, open PR (if `auto_pr`), flip to `needs_review` |
| `conflict`, still unresolved | highlight with conflict files + `git status` hint |
| `branch_created` (branch on origin, no PR) | push (if needed) + open PR |
| `needs_review` | leave |

Always finishes with a project-board reconcile.

```bash
releasy continue [--branch <branch-or-feature-id>] [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--branch <name>` | Operate on one entry — flip from `conflict` to `needs_review`. | full pass |
| `--work-dir <path>` | Working dir. | config / cwd |

Exit: `1` if any conflict remains (full pass) or the branch couldn't be
marked resolved.

### Sequential mode

When `sequential: true` is in `config.yaml`, both [`run`](#releasy-run)
and [`continue`](#releasy-continue) (without `--branch`) process **one
PR per invocation**. Queue is sorted by `merged_at`.

1. **First invocation** → port earliest PR, push, open rebase PR, exit.
2. **You** review, approve, merge that PR on GitHub.
3. **Next invocation** → checks GitHub:
   - Previous PR **merged** → mark `merged`, re-fetch, port the next.
   - Previous PR **not merged** → exit `1`, change nothing.
4. Repeat. AI-unresolvable conflict → stops; resolve manually + run
   `releasy continue --branch <id>`.

Constraints:
- Incompatible with `pr_sources.groups` (session load fails).
- Requires `target_branch:` in config.
- Re-run [`setup-project`](#releasy-setup-project) once to provision the
  new `merged` Status option.

```bash
releasy run
# (review, approve, merge on GitHub, then:)
releasy continue
```

### `releasy skip`

*Drop a conflicted port from this run.*

Marks `skipped` so subsequent passes ignore it. Doesn't touch git.

```bash
releasy skip --branch <branch-or-feature-id>
```

### `releasy abort`

*Stop tracking this run as in-progress.*

Persists state. No undo for ports already pushed — branches and PRs stay
exactly as they are.

```bash
releasy abort
```

## Inspection

### `releasy status`

*Print current pipeline state.*

Rich-text per-status sub-tables, ordered with conflicts first (see
`STATUS_DISPLAY_ORDER` in [`src/releasy/state.py`](../src/releasy/state.py)).
Reads state only — no git, no network.

```bash
releasy status
```

## Multi-project

See [concepts.md → Multiple projects](concepts.md#multiple-projects-in-parallel).

### `releasy new`

*Scaffold a fresh project.*

Writes `config.yaml` (at `--out`) + sibling `<name>.session.yaml`. Refuses
to overwrite. Prints config's absolute path on stdout (everything else on
stderr) so it composes:

```bash
cd $(dirname "$(releasy new --target-branch antalya-25.8 --project antalya)")
```

```bash
releasy new [--name <slug>] [--target-branch <branch>] [--project <id>] [--out <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--name <slug>` | `[A-Za-z0-9._-]{1,64}`. | auto: `<target-branch>-<6hex>` |
| `--target-branch <branch>` | Seeds `target_branch:` + auto-name. | empty |
| `--project <id>` | Seeds `project:`. | empty |
| `--out <path>` | Config path. Refuses to overwrite. | `./config.yaml` |

Auto-generated names get a 6-hex CSPRNG suffix so back-to-back calls don't
collide.

### `releasy list`

*Every project on this machine.* Alias: `releasy ls`.

One row per project: name, phase, feature counts, last-run timestamp,
owning config path.

```bash
releasy list
```

### `releasy where`

*Print the state-file path for the current config.*

```bash
releasy where
# /home/<you>/.local/state/releasy/antalya-26.3.state.yaml
```

### `releasy adopt`

*Rebind state to the current config.*

After moving/renaming a `config.yaml`, the next mutating command trips an
ownership-collision check. Run `adopt` from the new location to rebind;
the old path is appended to a history list for audit.

If no state exists yet, creates an empty one — doubles as "register this
config without doing anything else".

```bash
releasy adopt
```

## Project board sync

No-ops unless `notifications.github_project` is set and
`RELEASY_GITHUB_TOKEN` has `project` scope. UI setup:
[configuration.md → GitHub Project board](configuration.md#github-project-board).

### `releasy setup-project`

*Create / verify the GitHub Project.*

If configured: verifies project, reconciles Status options to the
canonical set, provisions `AI Cost`. If unset: creates a new project,
prints the URL, runs an initial sync.

```bash
releasy setup-project
```

> **Destructive:** drops non-canonical Status options. Cards on dropped
> options are re-synced based on local state immediately after.

### `releasy sync-project`

*Push local state to the project board.*

Reconciles every known feature: attaches missing PR cards, refreshes
existing, updates Status. No git, no PRs — only the board. Use after
hand-editing state, rotating tokens, or wiring up a new project URL.

```bash
releasy sync-project
```

Exit: `1` if sync was skipped (no project / no token / bad URL) or any
item failed, `0` otherwise.

## Release construction

### `releasy release`

*Build a release branch from a tag.*

Creates a release base branch from `--base-tag` and merges every finished
port (`needs_review`, optionally `skipped`) onto it.

```bash
releasy release --base-tag <tag> --name <branch> [--strict] [--include-skipped] [--work-dir <path>]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--base-tag <tag>` (required) | Tag/ref to base on. Must be local or fetchable from origin. | — |
| `--name <branch>` (required) | Release branch name. | — |
| `--strict` | Abort if any enabled feature isn't `needs_review`. | off |
| `--include-skipped` | Include `skipped` features. | off |
| `--work-dir <path>` | Working dir. | config / cwd |

## Feature management

Manages the static `features:` list in the session file (the dynamic
counterpart is `pr_sources.*`). Schema:
[configuration.md](configuration.md#namesessionyaml-per-effort-source-data).

```bash
releasy feature add --id <id> --source-branch <branch> --description <desc>
releasy feature enable --id <id>
releasy feature disable --id <id>
releasy feature remove --id <id>
releasy feature list
```

| Subcommand | Description |
|------------|-------------|
| `add` | Append entry. Requires `--id`, `--source-branch`, `--description`. |
| `enable` | Set `enabled: true`. Requires `--id`. |
| `disable` | Set `enabled: false`. Requires `--id`. |
| `remove` | Delete from session. Doesn't touch branches. Requires `--id`. |
| `list` | Print features grouped by enabled/disabled. |

# RelEasy

CLI tool for managing port branches, feature branches, and release construction.

RelEasy automates porting features and PRs onto a stable **base branch** (a
tag/commit you pin to) inside a single repo. Instead of rebasing long-lived
branches (which accumulates conflicts), each feature / PR is cherry-picked onto
its own port branch and opened as a PR — all in a resumable workflow.

A single machine can drive **multiple ongoing porting projects in parallel**
(e.g. an antalya-26.3 forward-port and an antalya-25.8 backport). Each project
has its own `config.yaml` with a unique `name:`; pipeline state lives outside
your repo under `${XDG_STATE_HOME:-~/.local/state}/releasy/<name>.state.yaml`,
and a per-project lock keeps concurrent runs of the same project serialized
while runs of *different* projects truly run in parallel.

Each project spreads across three files with clear responsibilities:

| File | What | Lifetime |
|------|------|----------|
| `config.yaml` | Stable infrastructure: origin remote, work_dir, target branch, AI settings, notifications, `pr_policy`. | Edited once at setup, rarely touched. |
| `<name>.session.yaml` | Per-effort source data: `features:` list, `pr_sources:` selectors (labels, include/exclude PR URLs, groups, author filters). Lives next to `config.yaml` by default; point elsewhere with `session_file:` in config or `--session-file` on the CLI. | Edited between runs as your target work changes. |
| `<name>.state.yaml` | Runtime progress managed by RelEasy. Lives under `${XDG_STATE_HOME:-~/.local/state}/releasy/`. | Never edited by hand. |

`releasy --stateless` (e.g. `address-review --stateless`) loads `config.yaml`
but skips the session and state files — useful for one-off runs where CLI flags
supply everything session data would otherwise provide.

## Install

```bash
pip install -e .
```

## TL;DR

```bash
export RELEASY_GITHUB_TOKEN="ghp_..."

mkdir -p ~/work/antalya-26.3 && cd ~/work/antalya-26.3
releasy new --target-branch antalya-26.3 --project antalya
$EDITOR config.yaml                     # origin remote, work_dir, push: true, …
$EDITOR antalya-26.3.session.yaml       # features + pr_sources
releasy run
```

See [`config.yaml.example`](config.yaml.example) and
[`session.yaml.example`](session.yaml.example) for fully-documented references.

## How it works

```
origin/antalya-26.3:          * (stable base branch on origin — you maintain it)
                              |
feature/antalya-26.3/pr-42:   * --- fix   (PR → antalya-26.3)
feature/antalya-26.3/pr-99:   * --- feat  (PR → antalya-26.3)
```

Given an existing base branch on origin, `releasy run`:

1. Discovers PRs from `pr_sources` in the session file (labels, explicit
   include/exclude lists, groups).
2. For each PR / group, creates a port branch `feature/<base>/<id>` from the
   base.
3. Cherry-picks the PR merge commit(s) onto the port branch.
4. Pushes and opens a PR into the base (if `push: true` and
   `pr_policy.auto_pr: true` — the default).

On conflict, the pipeline stops with instructions. Resolve, run
`releasy continue`, then `releasy run` again to resume with the remaining PRs.

## Documentation

- **[docs/concepts.md](docs/concepts.md)** — the mental model: pipeline,
  branch naming, multi-project layout, files RelEasy reads & writes, conflict
  handling, PR titles & labels, the "PRs always target origin" safety guarantee.
- **[docs/configuration.md](docs/configuration.md)** — full `config.yaml` and
  session-file schemas, the complete key-options table, environment variables,
  per-PR `ai_context`, GitHub Project board setup.
- **[docs/commands.md](docs/commands.md)** — every CLI subcommand with options,
  examples, and the "which command does what" matrix
  (`run` / `continue` / `refresh` / `discover-deps` / `analyze-fails` /
  `address-review` / `release` / `feature *` / project-board sync / sequential mode).

## License

See [LICENSE](LICENSE).

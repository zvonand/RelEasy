"""CLI entry point using Click."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import click

from releasy import __version__
from releasy.config import (
    Config,
    state_file_path,
    state_root,
    validate_project_name,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_config_or_exit(config_path: str | None = None) -> Config:
    from releasy.config import load_config

    path = Path(config_path) if config_path else None
    try:
        return load_config(path)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to load config: {e}")


def _load_and_verify(ctx: click.Context) -> Config:
    """Load config and verify the state file (if any) belongs to it.

    Use this for commands that need a config but don't take the project
    lock (read-only operations, or commands that explicitly rebind state).
    """
    from releasy.state import OwnershipCollisionError, verify_ownership

    config = _load_config_or_exit(ctx.obj["config_path"])
    try:
        verify_ownership(config)
    except OwnershipCollisionError as e:
        raise click.ClickException(str(e))
    return config


@contextmanager
def _locked_config(ctx: click.Context) -> Iterator[Config]:
    """Load + verify + lock a project's config; yield the Config.

    Wrap every mutating subcommand in this so concurrent invocations on
    the SAME project (same ``name:``) serialize, while invocations on
    different projects run in parallel.
    """
    from releasy.locks import project_lock

    config = _load_and_verify(ctx)
    with project_lock(config):
        yield config


def _short_id() -> str:
    """6 hex chars from a CSPRNG — used to disambiguate auto-generated names."""
    return secrets.token_hex(3)


def _render_template(text: str, **vars: str) -> str:
    """Tiny ``{{ key }}`` substitution. Whitespace around the key is tolerated."""
    out = text
    for key, value in vars.items():
        for placeholder in (f"{{{{ {key} }}}}", f"{{{{{key}}}}}"):
            out = out.replace(placeholder, value)
    return out


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


# Click defaults `max_content_width` to 80 even on wider terminals, which
# truncates our one-line command summaries with "..." in `releasy --help`.
# Bumping it lets the help output use the full terminal width (Click takes
# `min(max_content_width, terminal_width)`), so descriptions stay readable
# at modern terminal sizes without us having to artificially shorten them.
_CLI_CONTEXT_SETTINGS = {"max_content_width": 120}


@click.group(context_settings=_CLI_CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="releasy")
@click.option(
    "--config",
    "--config-file",
    "config_path",
    default=None,
    help="Path to config.yaml (defaults to ./config.yaml in the current directory)",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """RelEasy — manage port branches and release construction."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ---------------------------------------------------------------------------
# Maintenance pipeline
# ---------------------------------------------------------------------------


@cli.command(short_help="Discover + port new PRs (cherry-pick + open PR).")
@click.option(
    "--onto",
    default=None,
    help="Version label used to derive the base branch name "
         "(<project>-<version>). Just a string — never resolved as a git "
         "ref; the base branch must already exist on origin. Not needed "
         "when 'target_branch' is set in config.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    default=True,
    help="Invoke the AI resolver on conflicts (requires ai_resolve.enabled in config). "
         "Default: on.",
)
@click.pass_context
def run(
    ctx: click.Context,
    onto: str | None,
    work_dir: str | None,
    resolve_conflicts: bool,
) -> None:
    """Discover and port new PRs onto the base branch (cherry-pick + open PR)."""
    from releasy.pipeline import run_pipeline, run_sequential

    with _locked_config(ctx) as config:
        if not onto:
            if not config.target_branch:
                raise click.ClickException(
                    "Either pass --onto <ref> or set 'target_branch:' in config.yaml."
                )
            onto = config.target_branch

        wd = Path(work_dir) if work_dir else None

        if config.sequential:
            run_sequential(config, onto, wd, resolve_conflicts=resolve_conflicts)
            return

        state = run_pipeline(config, onto, wd, resolve_conflicts=resolve_conflicts)

        has_conflicts = any(
            fs.status == "conflict" for fs in state.features.values()
        )
        if has_conflicts:
            raise SystemExit(1)


@cli.command(
    name="continue",
    short_help="Reconcile state after manual fixes.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch or feature ID to mark resolved. If omitted, reconciles "
         "every port in state: opens PRs for any clean branch that lacks "
         "one (e.g. previous run had auto_pr off), pushes + opens PRs for "
         "newly-resolved conflicts, highlights any still-unresolved ones, "
         "and refreshes the GitHub Project board.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def continue_cmd(ctx: click.Context, branch: str | None, work_dir: str | None) -> None:
    """Reconcile state after a manual fix (push + open any missing PRs)."""
    from releasy.pipeline import continue_all, continue_branch, run_sequential

    with _locked_config(ctx) as config:
        wd = Path(work_dir) if work_dir else None
        if branch:
            if not continue_branch(config, branch):
                raise SystemExit(1)
            return

        if config.sequential:
            if not config.target_branch:
                raise click.ClickException(
                    "Sequential mode requires 'target_branch:' to be set in config.yaml."
                )
            run_sequential(
                config, config.target_branch, wd, resolve_conflicts=True,
            )
            return

        if not continue_all(config, wd):
            raise SystemExit(1)


@cli.command(short_help="Mark a port as skipped (state-only).")
@click.option("--branch", required=True, help="Branch name or feature ID to skip")
@click.pass_context
def skip(ctx: click.Context, branch: str) -> None:
    """Mark a port branch as skipped (state-only, branch + PR untouched)."""
    from releasy.pipeline import skip_branch

    with _locked_config(ctx) as config:
        if not skip_branch(config, branch):
            raise SystemExit(1)


@cli.command(short_help="Persist state and exit (nothing rolled back).")
@click.pass_context
def abort(ctx: click.Context) -> None:
    """Persist current state and exit (no rollback; branches/PRs untouched)."""
    from releasy.pipeline import abort_run

    with _locked_config(ctx) as config:
        abort_run(config)


@cli.command(short_help="Print current pipeline state (read-only).")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print current pipeline state (read-only, no git/network)."""
    from releasy.pipeline import print_status

    config = _load_and_verify(ctx)
    print_status(config)


@cli.command(
    name="import",
    short_help="Rebuild project state from GitHub + the project board.",
)
@click.pass_context
def import_cmd(ctx: click.Context) -> None:
    """Rebuild the per-project state file from GitHub + the GitHub Project board.

    Use this when the local state is missing or out of date (fresh
    machine, teammate takeover, throwaway CI runner) but the rest of the
    world is unchanged — source PRs still live on GitHub, rebase PRs are
    still open on origin, and the configured project board still carries
    the Skipped / AI Cost history.

    Read-only on git: no checkouts, no clones, no pushes, no new PRs.
    The command only hits the GitHub REST / GraphQL APIs. It merges into
    any existing state file — local-only fields (ai_iterations,
    failed_step_index, partial_pr_count) are preserved verbatim; the
    board wins for `Skipped` decisions and `AI Cost`; every other field
    is refreshed from the authoritative source.

    Requires notifications.github_project in config — without a project
    board there's no durable source for the Skipped / cost values that
    can't be re-derived from PRs alone.
    """
    from releasy.import_state import import_from_github

    with _locked_config(ctx) as config:
        if not import_from_github(config):
            raise SystemExit(1)


@cli.command(short_help="Merge target branch into each tracked PR.")
@click.option(
    "--work-dir", default=None, help="Working directory for git operations",
)
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    default=True,
    help="Invoke the AI resolver on merge conflicts (requires "
         "ai_resolve.enabled in config). With --no-resolve-conflicts, "
         "any tracked PR that conflicts with the target branch is just "
         "flagged in state without attempting an automatic fix. "
         "Default: on.",
)
@click.pass_context
def refresh(
    ctx: click.Context, work_dir: str | None, resolve_conflicts: bool,
) -> None:
    """Merge target branch into each tracked PR (AI-resolves conflicts).

    Strictly a maintenance pass — never opens new PRs, never creates
    new branches, never discovers new PR sources. Only operates on
    entries already present in the project state file.

    For every PR tracked in state that has a rebase PR open and a
    branch on origin: fetch the latest target + PR branch, attempt
    ``git merge --no-ff origin/<base>`` into the PR branch, and:

    - clean merge: leave the PR alone (use GitHub's *Update branch* if
      you want a fresh merge commit pushed),
    - conflict + AI resolves it: push the resolved merge commit
      (status preserved, ``ai_resolved`` flag set),
    - conflict + AI gives up / disabled: abort the merge, hard-reset
      the local branch back to its original tip, mark the entry as
      ``conflict`` in state + project board.

    Exit code is 1 if any PR ended up in conflict status, 0 otherwise —
    suitable for cron / CI loops.
    """
    from releasy.refresh import refresh_tracked_prs

    with _locked_config(ctx) as config:
        wd = Path(work_dir) if work_dir else None
        if not refresh_tracked_prs(config, wd, resolve_conflicts=resolve_conflicts):
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


@cli.command(short_help="Build a release branch from a tag.")
@click.option(
    "--base-tag", "base_tag", required=True,
    help="Tag/ref to base the release on (must be present locally or "
         "fetchable from origin)",
)
@click.option("--name", required=True, help="Release branch name")
@click.option("--strict", is_flag=True, help="Abort if any enabled feature is not ok")
@click.option("--include-skipped", is_flag=True, help="Include skipped features in release")
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def release(
    ctx: click.Context,
    base_tag: str,
    name: str,
    strict: bool,
    include_skipped: bool,
    work_dir: str | None,
) -> None:
    """Build a release branch from a tag, merging finished ports onto it."""
    from releasy.release import build_release

    with _locked_config(ctx) as config:
        wd = Path(work_dir) if work_dir else None
        if not build_release(config, base_tag, name, strict, include_skipped, wd):
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------------


@cli.command(
    name="setup-project",
    short_help="Create or verify the GitHub Project board.",
)
@click.pass_context
def setup_project_cmd(ctx: click.Context) -> None:
    """Create or verify a GitHub Project for status tracking.

    If notifications.github_project is set in config, verifies the project
    and its Status field. Otherwise, creates a new project and prints the URL
    to add to config.

    The Status field is fully owned by RelEasy: any options that aren't
    in the canonical set (Needs Review, Branch Created, Conflict,
    Skipped) get dropped on every run. After dropping orphan options,
    this command also triggers a project sync so any cards that were
    sitting on a now-removed option get re-assigned to the right Status
    based on local state.
    """
    from releasy.github_ops import setup_project
    from releasy.pipeline import sync_to_project

    with _locked_config(ctx) as config:
        url = setup_project(config)
        if not url:
            raise SystemExit(1)
        click.echo(f"Project ready: {url}")
        if not config.notifications.github_project:
            click.echo(
                f"\nAdd this to your config.yaml:\n\n"
                f"notifications:\n"
                f"  github_project: {url}\n"
            )
            return
        sync_to_project(config)


@cli.command(
    name="sync-project",
    short_help="Push local state to the Project board.",
)
@click.pass_context
def sync_project_cmd(ctx: click.Context) -> None:
    """Push the current local state to the GitHub Project board.

    Reads the per-project state file and reconciles every known feature
    with the configured project: attaches any missing PR cards, refreshes
    existing ones, and updates their Status field. No git operations,
    no PRs — just the project board.
    """
    from releasy.pipeline import sync_to_project

    with _locked_config(ctx) as config:
        if not sync_to_project(config):
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Multi-project ergonomics
# ---------------------------------------------------------------------------


@cli.command(name="new", short_help="Scaffold a fresh config from the bundled template.")
@click.option(
    "--name",
    "name_opt",
    default=None,
    help="Project name (slug). Auto-generated from --target-branch + a 6-hex "
         "id when omitted.",
)
@click.option(
    "--target-branch",
    "target_branch",
    default=None,
    help="Target/base branch this project will port onto (e.g. antalya-26.3). "
         "Used to seed `target_branch:` in the config and, when --name is "
         "omitted, to derive the auto-generated name (`<target-branch>-<6hex>`).",
)
@click.option(
    "--project",
    "project_opt",
    default="",
    help="Short project identifier used in derived branch names "
         "(e.g. antalya). Left blank when not given so you can fill it in.",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=Path),
    help="Where to write the new config. Defaults to ./config.yaml; refuses "
         "to overwrite an existing file.",
)
def new_cmd(
    name_opt: str | None,
    target_branch: str | None,
    project_opt: str,
    out_path: Path | None,
) -> None:
    """Scaffold a new releasy config and print its absolute path.

    Prints ONLY the absolute path on stdout, so it composes:

        cd $(dirname "$(releasy new --target-branch antalya-25.8)")
    """
    if name_opt is None:
        suffix = _short_id()
        if target_branch:
            name_opt = f"{target_branch}-{suffix}"
        else:
            name_opt = f"releasy-{suffix}"
    try:
        validate_project_name(name_opt)
    except ValueError as e:
        raise click.ClickException(str(e))

    if out_path is None:
        out_path = Path.cwd() / "config.yaml"
    out_path = out_path.expanduser().resolve()
    if out_path.exists():
        raise click.ClickException(
            f"{out_path} already exists. Pass --out to write somewhere else "
            f"or remove the existing file first."
        )

    template_path = Path(__file__).parent / "templates" / "config.yaml.tmpl"
    if not template_path.exists():
        raise click.ClickException(
            f"Bundled template missing: {template_path}. This is a packaging "
            "bug — please report it."
        )
    text = template_path.read_text()
    rendered = _render_template(
        text,
        name=name_opt,
        target_branch=target_branch or "",
        project=project_opt or "",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered)

    # stdout is the absolute path and nothing else; user-facing chatter
    # goes to stderr so shell composition (cd $(releasy new …)) works.
    click.echo(str(out_path))
    click.echo(
        f"Created config for project {name_opt!r}. "
        f"State will live at {state_file_path(name_opt)}.",
        err=True,
    )


@cli.command(name="list", short_help="List every releasy project on this machine.")
def list_cmd() -> None:
    """List every project found under the user's state dir.

    Outputs one row per project: ``name | phase | features | last_run | config``.
    """
    from rich.console import Console
    from rich.table import Table

    from releasy.state import _read_raw_state  # internal helper, see state.py

    root = state_root()
    state_files = sorted(root.glob("*.state.yaml"))
    if not state_files:
        click.echo(f"No projects found under {root}.")
        click.echo(
            "Use `releasy new` to scaffold one, then `releasy run` to "
            "kick off the pipeline."
        )
        return

    table = Table(title=f"RelEasy projects ({root})", title_justify="left")
    table.add_column("Name", style="cyan")
    table.add_column("Phase")
    table.add_column("Features")
    table.add_column("Last run")
    table.add_column("Config")

    for path in state_files:
        name = path.name[: -len(".state.yaml")]
        raw = _read_raw_state(path)
        run_blob = raw.get("last_run") or {}
        phase = run_blob.get("phase") or "—"
        features = (run_blob.get("features") or {})
        ok = sum(
            1 for f in features.values()
            if (f or {}).get("status") in ("needs_review", "branch_created")
        )
        conflict = sum(
            1 for f in features.values()
            if (f or {}).get("status") == "conflict"
        )
        skipped = sum(
            1 for f in features.values()
            if (f or {}).get("status") == "skipped"
        )
        feat_summary = (
            f"{ok} ok / {conflict} conflict"
            + (f" / {skipped} skipped" if skipped else "")
        )
        table.add_row(
            name,
            phase,
            feat_summary,
            run_blob.get("started_at") or "—",
            raw.get("config_path") or "—",
        )
    Console().print(table)


# Register `releasy ls` as an alias for `releasy list`. We add it as a
# separate Click command (rather than `aliases=`, which Click doesn't
# support natively) so help output shows both names.
@cli.command(name="ls", short_help="Alias for `releasy list`.")
def ls_cmd() -> None:
    """Alias for `releasy list`."""
    list_cmd.callback()  # type: ignore[misc]


@cli.command(short_help="Print the state file path for the resolved config.")
@click.pass_context
def where(ctx: click.Context) -> None:
    """Print the absolute path of this project's state file."""
    config = _load_config_or_exit(ctx.obj["config_path"])
    click.echo(str(config.state_path))


@cli.command(short_help="Rebind state to the current config (use after moving config).")
@click.pass_context
def adopt(ctx: click.Context) -> None:
    """Rewrite the state file's stored config_path to the current config.

    Use after moving / renaming your config.yaml so subsequent commands
    don't trip the ownership-collision check. Creates an empty state
    file if none exists yet (so `releasy adopt` doubles as "register
    this config without doing anything").
    """
    from releasy.locks import project_lock
    from releasy.state import adopt_ownership

    config = _load_config_or_exit(ctx.obj["config_path"])
    with project_lock(config):
        previous, state_path = adopt_ownership(config)
    if previous is None:
        click.echo(
            f"Project {config.name!r} is now bound to "
            f"{config.config_path}.\nState file: {state_path}"
        )
    else:
        click.echo(
            f"Rebound project {config.name!r} from {previous} to "
            f"{config.config_path}.\nState file: {state_path}"
        )


# ---------------------------------------------------------------------------
# Feature management
# ---------------------------------------------------------------------------


@cli.group()
def feature() -> None:
    """Manage the static `features:` list in config.yaml."""


@feature.command(name="add")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.option("--source-branch", required=True, help="Existing branch with feature commits")
@click.option("--description", required=True, help="Feature description")
@click.pass_context
def feature_add(
    ctx: click.Context, feature_id: str, source_branch: str, description: str,
) -> None:
    """Add a new feature branch."""
    from releasy.feature import add_feature

    with _locked_config(ctx) as config:
        if not add_feature(config, feature_id, source_branch, description):
            raise SystemExit(1)


@feature.command(name="enable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_enable(ctx: click.Context, feature_id: str) -> None:
    """Enable a feature branch."""
    from releasy.feature import enable_feature

    with _locked_config(ctx) as config:
        if not enable_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="disable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_disable(ctx: click.Context, feature_id: str) -> None:
    """Disable a feature branch."""
    from releasy.feature import disable_feature

    with _locked_config(ctx) as config:
        if not disable_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="remove")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_remove(ctx: click.Context, feature_id: str) -> None:
    """Remove a feature branch."""
    from releasy.feature import remove_feature

    with _locked_config(ctx) as config:
        if not remove_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="list")
@click.pass_context
def feature_list(ctx: click.Context) -> None:
    """List all configured features."""
    from releasy.feature import list_features

    config = _load_and_verify(ctx)
    list_features(config)



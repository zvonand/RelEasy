"""CLI entry point using Click."""

from __future__ import annotations

from pathlib import Path

import click

from releasy import __version__


def _load_config_or_exit(config_path: str | None = None) -> "Config":
    from releasy.config import load_config

    path = Path(config_path) if config_path else None
    try:
        return load_config(path)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to load config: {e}")


# Click defaults `max_content_width` to 80 even on wider terminals, which
# truncates our one-line command summaries with "..." in `releasy --help`.
# Bumping it lets the help output use the full terminal width (Click takes
# `min(max_content_width, terminal_width)`), so descriptions stay readable
# at modern terminal sizes without us having to artificially shorten them.
_CLI_CONTEXT_SETTINGS = {"max_content_width": 120}


@click.group(context_settings=_CLI_CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="releasy")
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """RelEasy — manage port branches and release construction."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ---------- Maintenance pipeline ----------


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
    from releasy.pipeline import run_pipeline

    config = _load_config_or_exit(ctx.obj["config_path"])

    if not onto:
        if not config.target_branch:
            raise click.ClickException(
                "Either pass --onto <ref> or set 'target_branch:' in config.yaml."
            )
        onto = config.target_branch

    wd = Path(work_dir) if work_dir else None
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
    from releasy.pipeline import continue_all, continue_branch

    config = _load_config_or_exit(ctx.obj["config_path"])
    wd = Path(work_dir) if work_dir else None
    if branch:
        if not continue_branch(config, branch):
            raise SystemExit(1)
    else:
        if not continue_all(config, wd):
            raise SystemExit(1)


@cli.command(short_help="Mark a port as skipped (state-only).")
@click.option("--branch", required=True, help="Branch name or feature ID to skip")
@click.pass_context
def skip(ctx: click.Context, branch: str) -> None:
    """Mark a port branch as skipped (state-only, branch + PR untouched)."""
    from releasy.pipeline import skip_branch

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not skip_branch(config, branch):
        raise SystemExit(1)


@cli.command(short_help="Persist state and exit (nothing rolled back).")
@click.pass_context
def abort(ctx: click.Context) -> None:
    """Persist current state and exit (no rollback; branches/PRs untouched)."""
    from releasy.pipeline import abort_run

    config = _load_config_or_exit(ctx.obj["config_path"])
    abort_run(config)


@cli.command(short_help="Print current pipeline state (read-only).")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print current pipeline state (read-only, no git/network)."""
    from releasy.pipeline import print_status

    config = _load_config_or_exit(ctx.obj["config_path"])
    print_status(config)


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
    entries already present in state.yaml.

    For every PR tracked in state.yaml that has a rebase PR open and a
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

    config = _load_config_or_exit(ctx.obj["config_path"])
    wd = Path(work_dir) if work_dir else None
    if not refresh_tracked_prs(config, wd, resolve_conflicts=resolve_conflicts):
        raise SystemExit(1)


# ---------- Release ----------


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

    config = _load_config_or_exit(ctx.obj["config_path"])
    wd = Path(work_dir) if work_dir else None
    if not build_release(config, base_tag, name, strict, include_skipped, wd):
        raise SystemExit(1)


# ---------- Project setup ----------


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

    config = _load_config_or_exit(ctx.obj["config_path"])
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
        # No project URL in config yet → nothing to sync against.
        return
    # Re-sync items so anything that was sitting on a dropped option
    # (e.g. legacy "Ok" / "Resolved") lands on the right Status.
    sync_to_project(config)


@cli.command(
    name="sync-project",
    short_help="Push local state to the Project board.",
)
@click.pass_context
def sync_project_cmd(ctx: click.Context) -> None:
    """Push the current local state to the GitHub Project board.

    Reads state.yaml and reconciles every known feature with the
    configured project: attaches any missing PR cards, refreshes existing
    ones, and updates their Status field. No git operations, no PRs —
    just the project board.
    """
    from releasy.pipeline import sync_to_project

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not sync_to_project(config):
        raise SystemExit(1)


# ---------- Feature management ----------


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

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not add_feature(config, feature_id, source_branch, description):
        raise SystemExit(1)


@feature.command(name="enable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_enable(ctx: click.Context, feature_id: str) -> None:
    """Enable a feature branch."""
    from releasy.feature import enable_feature

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not enable_feature(config, feature_id):
        raise SystemExit(1)


@feature.command(name="disable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_disable(ctx: click.Context, feature_id: str) -> None:
    """Disable a feature branch."""
    from releasy.feature import disable_feature

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not disable_feature(config, feature_id):
        raise SystemExit(1)


@feature.command(name="remove")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_remove(ctx: click.Context, feature_id: str) -> None:
    """Remove a feature branch."""
    from releasy.feature import remove_feature

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not remove_feature(config, feature_id):
        raise SystemExit(1)


@feature.command(name="list")
@click.pass_context
def feature_list(ctx: click.Context) -> None:
    """List all configured features."""
    from releasy.feature import list_features

    config = _load_config_or_exit(ctx.obj["config_path"])
    list_features(config)

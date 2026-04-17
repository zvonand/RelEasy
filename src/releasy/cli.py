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


@click.group()
@click.version_option(version=__version__, prog_name="releasy")
@click.option("--config", "config_path", default=None, help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """RelEasy — manage fork rebases and release construction."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ---------- Maintenance pipeline ----------


@cli.command()
@click.option(
    "--onto",
    default=None,
    help="Upstream ref/tag used to derive the base branch name "
         "(<project>-<version>). Not needed when 'target_branch' is set in config.",
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
    """Port PRs onto the already-existing base branch."""
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


@cli.command(name="continue")
@click.option(
    "--branch",
    default=None,
    help="Branch or feature ID to mark resolved. If omitted, processes "
         "every port in state: pushes + opens PRs for resolved ones, "
         "highlights any still-unresolved conflicts.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def continue_cmd(ctx: click.Context, branch: str | None, work_dir: str | None) -> None:
    """Continue after manual conflict resolution."""
    from releasy.pipeline import continue_all, continue_branch

    config = _load_config_or_exit(ctx.obj["config_path"])
    wd = Path(work_dir) if work_dir else None
    if branch:
        if not continue_branch(config, branch):
            raise SystemExit(1)
    else:
        if not continue_all(config, wd):
            raise SystemExit(1)


@cli.command()
@click.option("--branch", required=True, help="Branch name or feature ID to skip")
@click.pass_context
def skip(ctx: click.Context, branch: str) -> None:
    """Skip a conflicted branch for this run."""
    from releasy.pipeline import skip_branch

    config = _load_config_or_exit(ctx.obj["config_path"])
    if not skip_branch(config, branch):
        raise SystemExit(1)


@cli.command()
@click.pass_context
def abort(ctx: click.Context) -> None:
    """Abort the current run, leaving all branches as-is."""
    from releasy.pipeline import abort_run

    config = _load_config_or_exit(ctx.obj["config_path"])
    abort_run(config)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print current pipeline state."""
    from releasy.pipeline import print_status

    config = _load_config_or_exit(ctx.obj["config_path"])
    print_status(config)


# ---------- Release ----------


@cli.command()
@click.option("--upstream-tag", required=True, help="Upstream tag to base release on")
@click.option("--name", required=True, help="Release branch name")
@click.option("--strict", is_flag=True, help="Abort if any enabled feature is not ok")
@click.option("--include-skipped", is_flag=True, help="Include skipped features in release")
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def release(
    ctx: click.Context,
    upstream_tag: str,
    name: str,
    strict: bool,
    include_skipped: bool,
    work_dir: str | None,
) -> None:
    """Create release base branch + per-feature PRs from an upstream tag."""
    from releasy.release import build_release

    config = _load_config_or_exit(ctx.obj["config_path"])
    wd = Path(work_dir) if work_dir else None
    if not build_release(config, upstream_tag, name, strict, include_skipped, wd):
        raise SystemExit(1)


# ---------- Project setup ----------


@cli.command(name="setup-project")
@click.pass_context
def setup_project_cmd(ctx: click.Context) -> None:
    """Create or verify a GitHub Project for status tracking.

    If notifications.github_project is set in config, verifies the project
    and its Status field. Otherwise, creates a new project and prints the URL
    to add to config.
    """
    from releasy.github_ops import setup_project

    config = _load_config_or_exit(ctx.obj["config_path"])
    url = setup_project(config)
    if url:
        click.echo(f"Project ready: {url}")
        if not config.notifications.github_project:
            click.echo(
                f"\nAdd this to your config.yaml:\n\n"
                f"notifications:\n"
                f"  github_project: {url}\n"
            )
    else:
        raise SystemExit(1)


# ---------- Feature management ----------


@cli.group()
def feature() -> None:
    """Manage feature branches."""


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

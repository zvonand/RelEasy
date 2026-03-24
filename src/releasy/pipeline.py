"""Two-stage rebase pipeline: CI branch then feature branches."""

from __future__ import annotations

import tempfile
from pathlib import Path

from rich.console import Console

from releasy.config import Config
from releasy.git_ops import (
    checkout_branch,
    ensure_work_repo,
    fetch_all,
    force_push,
    rebase_onto,
    stash_and_clean,
)
from releasy.github_ops import commit_and_push_state, sync_project
from releasy.state import CIBranchState, FeatureState, PipelineState, load_state, save_state
from releasy.status import write_status_md

console = Console()


def _update_state_and_status(config: Config, state: PipelineState) -> None:
    """Persist state + STATUS.md, sync project board, and push to the tool repo."""
    save_state(state)
    write_status_md(config, state)
    sync_project(config, state)
    commit_and_push_state(f"releasy: update state — onto {state.onto}")


def run_pipeline(config: Config, onto: str, work_dir: Path | None = None) -> PipelineState:
    """Execute the full two-stage rebase pipeline.

    Stage 1: Rebase CI branch onto <onto>.
    Stage 2: Rebase each enabled feature branch onto the updated CI branch.
    """
    state = PipelineState()
    state.set_started(onto)

    for feat in config.features:
        if feat.enabled:
            state.features[feat.id] = FeatureState(status="pending")
        else:
            state.features[feat.id] = FeatureState(status="disabled")

    save_state(state)

    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="releasy-"))
    console.print(f"[dim]Working directory: {work_dir}[/dim]")

    repo_path = ensure_work_repo(config, work_dir)
    fetch_all(config, repo_path)

    # --- Stage 1: Rebase CI branch ---
    console.print(
        f"\n[bold]Stage 1:[/bold] Rebasing [cyan]{config.ci_branch}[/cyan] "
        f"onto [yellow]{onto}[/yellow]"
    )

    checkout_branch(repo_path, config.ci_branch, config.fork.remote_name)
    result = rebase_onto(repo_path, onto)

    if result.success:
        console.print(f"  [green]✓[/green] Rebase successful")
        force_push(repo_path, config.ci_branch, config.fork.remote_name)
        console.print(f"  [green]✓[/green] Force-pushed {config.ci_branch}")
        state.ci_branch = CIBranchState(status="ok", rebased_onto=onto)
        _update_state_and_status(config, state)
    else:
        console.print(f"  [red]✗[/red] Rebase conflict!")
        for f in result.conflict_files:
            console.print(f"    [red]•[/red] {f}")
        state.ci_branch = CIBranchState(
            status="conflict", conflict_files=result.conflict_files
        )
        _update_state_and_status(config, state)
        console.print("\n[red]Stage 1 failed. Stage 2 will not run.[/red]")
        return state

    # --- Stage 2: Rebase feature branches ---
    console.print(
        f"\n[bold]Stage 2:[/bold] Rebasing feature branches "
        f"onto [cyan]{config.ci_branch}[/cyan]"
    )

    for feat in config.enabled_features:
        console.print(f"\n  Rebasing [cyan]{feat.branch}[/cyan] ({feat.id})")

        stash_and_clean(repo_path)
        checkout_branch(repo_path, feat.branch, config.fork.remote_name)
        result = rebase_onto(repo_path, config.ci_branch)

        if result.success:
            console.print(f"    [green]✓[/green] Rebase successful")
            force_push(repo_path, feat.branch, config.fork.remote_name)
            console.print(f"    [green]✓[/green] Force-pushed {feat.branch}")
            state.features[feat.id] = FeatureState(status="ok", rebased_onto=onto)
        else:
            console.print(f"    [red]✗[/red] Rebase conflict!")
            for f in result.conflict_files:
                console.print(f"      [red]•[/red] {f}")
            state.features[feat.id] = FeatureState(
                status="conflict", conflict_files=result.conflict_files
            )

        _update_state_and_status(config, state)

    # Summary
    console.print("\n[bold]Pipeline complete.[/bold]")
    ok_count = sum(1 for fs in state.features.values() if fs.status == "ok")
    conflict_count = sum(1 for fs in state.features.values() if fs.status == "conflict")
    disabled_count = sum(1 for fs in state.features.values() if fs.status == "disabled")

    console.print(f"  Features: {ok_count} ok, {conflict_count} conflict, {disabled_count} disabled")

    return state


def continue_branch(config: Config, branch_name: str) -> bool:
    """Mark a previously-conflicted branch as resolved after operator fixes it."""
    state = load_state()

    if branch_name == config.ci_branch:
        if state.ci_branch.status != "conflict":
            console.print(
                f"[yellow]Branch {branch_name} is not in conflict state "
                f"(status: {state.ci_branch.status})[/yellow]"
            )
            return False
        state.ci_branch.status = "resolved"
        state.ci_branch.conflict_files = []
    else:
        feat = config.get_feature_by_branch(branch_name)
        if feat is None:
            feat = config.get_feature(branch_name)
        if feat is None:
            console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
            return False

        fs = state.features.get(feat.id)
        if fs is None or fs.status != "conflict":
            current = fs.status if fs else "unknown"
            console.print(
                f"[yellow]Feature {feat.id} is not in conflict state "
                f"(status: {current})[/yellow]"
            )
            return False

        state.features[feat.id].status = "resolved"
        state.features[feat.id].conflict_files = []

    _update_state_and_status(config, state)
    console.print(f"[green]✓[/green] Branch [cyan]{branch_name}[/cyan] marked as resolved")
    return True


def skip_branch(config: Config, branch_name: str) -> bool:
    """Mark a branch as skipped for this run."""
    state = load_state()

    if branch_name == config.ci_branch:
        state.ci_branch.status = "skipped"
        state.ci_branch.conflict_files = []
    else:
        feat = config.get_feature_by_branch(branch_name)
        if feat is None:
            feat = config.get_feature(branch_name)
        if feat is None:
            console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
            return False

        fs = state.features.get(feat.id)
        if fs is None:
            console.print(f"[red]No state found for feature {feat.id}[/red]")
            return False

        state.features[feat.id].status = "skipped"
        state.features[feat.id].conflict_files = []

    _update_state_and_status(config, state)
    console.print(f"[yellow]⏭[/yellow] Branch [cyan]{branch_name}[/cyan] marked as skipped")
    return True


def abort_run(config: Config) -> None:
    """Abort the current run, leaving all branches as-is."""
    state = load_state()
    console.print("[yellow]Aborting current run. All branches left as-is.[/yellow]")
    _update_state_and_status(config, state)


def print_status(config: Config) -> None:
    """Print the current pipeline state to stdout."""
    from rich.table import Table

    state = load_state()

    table = Table(title="RelEasy Branch Status")
    table.add_column("Branch", style="cyan")
    table.add_column("Status")
    table.add_column("Rebased Onto")
    table.add_column("Conflict Files", style="red")

    ci = state.ci_branch
    style_map = {
        "ok": "green", "conflict": "red", "resolved": "blue",
        "skipped": "yellow", "disabled": "dim", "pending": "dim",
    }

    status_style = style_map.get(ci.status, "white")
    table.add_row(
        config.ci_branch,
        f"[{status_style}]{ci.status}[/{status_style}]",
        ci.rebased_onto or "",
        ", ".join(ci.conflict_files),
    )

    for feat in config.features:
        fs = state.features.get(feat.id)
        if fs is None:
            status = "disabled" if not feat.enabled else "pending"
            table.add_row(feat.branch, f"[dim]{status}[/dim]", "", "")
            continue

        status_style = style_map.get(fs.status, "white")
        table.add_row(
            feat.branch,
            f"[{status_style}]{fs.status}[/{status_style}]",
            fs.rebased_onto or "",
            ", ".join(fs.conflict_files),
        )

    console.print()
    console.print(f"Last run: {state.started_at or 'N/A'}  ·  Onto: {state.onto or 'N/A'}")
    console.print(table)

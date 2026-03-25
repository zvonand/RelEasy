"""Three-stage pipeline: create clean branches and cherry-pick changes.

Stage 1: CI branch — create ci/<prefix>/<sha8> from upstream, cherry-pick CI commits.
Stage 2: Feature branches — create feature/<id>/<sha8> from CI branch, cherry-pick feature commits.
Stage 3: PR-based features — discover PRs by label, cherry-pick merge commits onto CI branch.

On subsequent runs, the previous versioned branch (from state) is used as the
source of commits. On the first run, the source_branch from config is used and
merge-base with upstream determines the divergence point.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from rich.console import Console

from releasy.config import Config, FeatureConfig
from releasy.git_ops import (
    cherry_pick_merge_commit,
    cherry_pick_range,
    count_commits,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_all,
    fetch_pr_ref,
    find_merge_base,
    force_push,
    get_branch_tip,
    get_short_sha,
    stash_and_clean,
)
from releasy.github_ops import commit_and_push_state, search_prs_by_label, sync_project
from releasy.state import CIBranchState, FeatureState, PipelineState, load_state, save_state
from releasy.status import write_status_md

console = Console()


def _carry_pr_metadata(prev_fs: FeatureState | None) -> dict:
    """Extract PR metadata fields from a previous FeatureState for carry-forward."""
    if not prev_fs or not prev_fs.pr_number:
        return {}
    return {
        "pr_url": prev_fs.pr_url,
        "pr_number": prev_fs.pr_number,
        "pr_title": prev_fs.pr_title,
        "pr_body": prev_fs.pr_body,
    }


def _update_state_and_status(config: Config, state: PipelineState) -> None:
    """Persist state + STATUS.md, sync project board, and push to the tool repo."""
    save_state(state, config.repo_dir)
    write_status_md(config, state)
    sync_project(config, state)
    commit_and_push_state(f"releasy: update state — onto {state.onto}", config.repo_dir)


def _resolve_source_branch(
    state_branch: str | None,
    config_source: str,
    fork_remote: str,
) -> str:
    """Determine the remote ref to cherry-pick from.

    If a previous versioned branch exists in state, use that.
    Otherwise fall back to the config's source_branch.
    """
    if state_branch:
        return f"{fork_remote}/{state_branch}"
    return f"{fork_remote}/{config_source}"


def run_pipeline(config: Config, onto: str, work_dir: Path | None = None) -> PipelineState:
    """Execute the full three-stage pipeline.

    Stage 1: Create clean CI branch from upstream <onto>, cherry-pick CI commits.
    Stage 2: Create clean feature branches from the new CI branch, cherry-pick feature commits.
    Stage 3: Discover PRs by label, cherry-pick merge commits onto feature branches.
    """
    prev_state = load_state(config.repo_dir)

    # Synthesize FeatureConfig entries for PR-sourced features from a previous run.
    # This makes Stage 2 handle them on subsequent runs (cherry-pick from previous
    # versioned branch) instead of Stage 3 re-cherry-picking the merge commit.
    for fid, prev_fs in prev_state.features.items():
        if prev_fs.pr_number and config.get_feature(fid) is None:
            config.features.append(FeatureConfig(
                id=fid,
                description=prev_fs.pr_title or f"PR #{prev_fs.pr_number}",
                source_branch="",
                enabled=True,
            ))

    state = PipelineState()
    state.set_started(onto)

    for feat in config.features:
        if feat.enabled:
            state.features[feat.id] = FeatureState(status="pending")
        else:
            state.features[feat.id] = FeatureState(status="disabled")

    save_state(state, config.repo_dir)

    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="releasy-"))
    console.print(f"[dim]Working directory: {work_dir}[/dim]")

    repo_path = ensure_work_repo(config, work_dir)
    fetch_all(config, repo_path)

    short_sha = get_short_sha(repo_path, onto)
    new_ci_branch = config.ci_branch_name(short_sha)

    # --- Stage 1: Create clean CI branch and cherry-pick CI commits ---
    console.print(
        f"\n[bold]Stage 1:[/bold] Creating [cyan]{new_ci_branch}[/cyan] "
        f"from [yellow]{onto[:12]}[/yellow] and cherry-picking CI commits"
    )

    # Determine source of CI commits
    source_ref = _resolve_source_branch(
        prev_state.ci_branch.branch_name,
        config.ci.source_branch,
        config.fork.remote_name,
    )

    # Find the divergence point: where the source branch parts from upstream
    if prev_state.ci_branch.base_commit:
        # We know exactly where the old branch was based — commits start right after that
        divergence = prev_state.ci_branch.base_commit
    else:
        # First run: find merge-base between source branch and the --onto ref
        divergence = find_merge_base(repo_path, source_ref, onto)
        if divergence is None:
            console.print(f"  [red]✗[/red] Could not find merge-base between {source_ref} and {onto}")
            state.ci_branch = CIBranchState(
                status="conflict", conflict_files=[],
            )
            _update_state_and_status(config, state)
            return state

    source_tip = get_branch_tip(repo_path, source_ref)
    n_commits = count_commits(repo_path, divergence, source_tip)
    console.print(f"  Source: [dim]{source_ref}[/dim] ({n_commits} commit(s) to apply)")

    # Create the clean branch
    stash_and_clean(repo_path)
    create_branch_from_ref(repo_path, new_ci_branch, onto)

    if n_commits > 0:
        result = cherry_pick_range(repo_path, divergence, source_tip)
        if result.success:
            console.print(f"  [green]✓[/green] Cherry-picked {n_commits} CI commit(s)")
        else:
            console.print(f"  [red]✗[/red] Conflict cherry-picking CI commits!")
            for f in result.conflict_files:
                console.print(f"    [red]•[/red] {f}")
            state.ci_branch = CIBranchState(
                status="conflict",
                branch_name=new_ci_branch,
                base_commit=onto,
                conflict_files=result.conflict_files,
            )
            _update_state_and_status(config, state)
            console.print("\n[red]Stage 1 failed. Stage 2 will not run.[/red]")
            return state
    else:
        console.print(f"  [dim]No CI commits to apply — branch is clean upstream[/dim]")

    # Push the new CI branch
    force_push(repo_path, new_ci_branch, config.fork.remote_name)
    console.print(f"  [green]✓[/green] Pushed [cyan]{new_ci_branch}[/cyan]")

    state.ci_branch = CIBranchState(
        status="ok",
        branch_name=new_ci_branch,
        base_commit=onto,
    )
    _update_state_and_status(config, state)

    # --- Stage 2: Create clean feature branches and cherry-pick ---
    console.print(
        f"\n[bold]Stage 2:[/bold] Creating feature branches "
        f"from [cyan]{new_ci_branch}[/cyan]"
    )

    for feat in config.enabled_features:
        new_feat_branch = config.feature_branch_name(feat.id, short_sha)
        console.print(f"\n  [cyan]{new_feat_branch}[/cyan] ({feat.id})")

        # Determine source of feature commits
        prev_feat = prev_state.features.get(feat.id)
        feat_source_ref = _resolve_source_branch(
            prev_feat.branch_name if prev_feat else None,
            feat.source_branch,
            config.fork.remote_name,
        )

        # Find divergence point: we always compare the feature branch against the
        # CI branch to isolate feature-only commits. Never against upstream — that
        # would incorrectly include CI commits in the cherry-pick range.
        if prev_feat and prev_feat.base_commit:
            # Subsequent run: the previous feature branch was created from the
            # previous CI branch. Feature-only commits = old_ci_tip..feat_tip.
            old_ci_ref = _resolve_source_branch(
                prev_state.ci_branch.branch_name,
                config.ci.source_branch,
                config.fork.remote_name,
            )
            feat_divergence = get_branch_tip(repo_path, old_ci_ref)
        else:
            # First run: the source_branch was forked from the CI source_branch
            # at some point. merge-base(feature, ci) finds that fork point.
            ci_source_ref = _resolve_source_branch(
                None, config.ci.source_branch, config.fork.remote_name,
            )
            feat_divergence = find_merge_base(repo_path, feat_source_ref, ci_source_ref)
            if feat_divergence is None:
                console.print(
                    f"    [red]✗[/red] Could not find divergence between "
                    f"{feat_source_ref} and CI branch {ci_source_ref}"
                )
                state.features[feat.id] = FeatureState(
                    status="conflict", branch_name=new_feat_branch, base_commit=onto,
                    **_carry_pr_metadata(prev_feat),
                )
                _update_state_and_status(config, state)
                continue

        feat_source_tip = get_branch_tip(repo_path, feat_source_ref)
        n_feat_commits = count_commits(repo_path, feat_divergence, feat_source_tip)
        console.print(f"    Source: [dim]{feat_source_ref}[/dim] ({n_feat_commits} commit(s))")

        # Create clean branch from CI branch
        stash_and_clean(repo_path)
        create_branch_from_ref(repo_path, new_feat_branch, new_ci_branch)

        if n_feat_commits > 0:
            result = cherry_pick_range(repo_path, feat_divergence, feat_source_tip)
            if result.success:
                console.print(f"    [green]✓[/green] Cherry-picked {n_feat_commits} commit(s)")
            else:
                console.print(f"    [red]✗[/red] Conflict!")
                for f in result.conflict_files:
                    console.print(f"      [red]•[/red] {f}")
                state.features[feat.id] = FeatureState(
                    status="conflict",
                    branch_name=new_feat_branch,
                    base_commit=onto,
                    conflict_files=result.conflict_files,
                    **_carry_pr_metadata(prev_feat),
                )
                _update_state_and_status(config, state)
                continue
        else:
            console.print(f"    [dim]No feature commits to apply[/dim]")

        force_push(repo_path, new_feat_branch, config.fork.remote_name)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_feat_branch}[/cyan]")

        state.features[feat.id] = FeatureState(
            status="ok",
            branch_name=new_feat_branch,
            base_commit=onto,
            **_carry_pr_metadata(prev_feat),
        )
        _update_state_and_status(config, state)

    # --- Stage 3: PR-based features (bootstrap new PRs only) ---
    # PRs already known from a previous run were synthesized into config.features
    # above and processed by Stage 2. Stage 3 only handles newly discovered PRs.
    if config.pr_sources:
        console.print(
            f"\n[bold]Stage 3:[/bold] PR-based features "
            f"from [cyan]{new_ci_branch}[/cyan]"
        )

        existing_ids = {feat.id for feat in config.features}

        for pr_source in config.pr_sources:
            console.print(f"\n  Searching for PRs with label [yellow]{pr_source.label}[/yellow]")
            prs = search_prs_by_label(config, pr_source.label)

            if not prs:
                console.print(f"    [dim]No PRs found[/dim]")
                continue

            console.print(f"    Found {len(prs)} PR(s)")

            for pr in prs:
                feature_id = f"pr-{pr.number}"

                if feature_id in existing_ids:
                    # Already handled by Stage 2 (from previous state) or
                    # conflicts with a static feature — skip.
                    continue

                new_pr_branch = config.feature_branch_name(feature_id, short_sha)
                desc = pr.title
                if pr_source.description:
                    desc = f"{pr_source.description}: {desc}"

                console.print(f"\n    [cyan]{new_pr_branch}[/cyan] (PR #{pr.number}: {pr.title})")
                console.print(f"      PR: {pr.url}  [{pr.state}]")

                pr_meta = {
                    "pr_url": pr.url,
                    "pr_number": pr.number,
                    "pr_title": pr.title,
                    "pr_body": pr.body,
                }

                state.features[feature_id] = FeatureState(status="pending", **pr_meta)

                config.features.append(FeatureConfig(
                    id=feature_id,
                    description=desc,
                    source_branch="",
                    enabled=True,
                ))

                existing_ids.add(feature_id)

                stash_and_clean(repo_path)
                create_branch_from_ref(repo_path, new_pr_branch, new_ci_branch)

                if pr.state == "merged" and pr.merge_commit_sha:
                    cp_result = cherry_pick_merge_commit(repo_path, pr.merge_commit_sha)
                else:
                    if not fetch_pr_ref(repo_path, config.fork.remote_name, pr.number):
                        console.print(
                            f"      [red]✗[/red] Could not fetch merge ref for PR #{pr.number}"
                        )
                        state.features[feature_id] = FeatureState(
                            status="conflict",
                            branch_name=new_pr_branch,
                            base_commit=onto,
                            **pr_meta,
                        )
                        _update_state_and_status(config, state)
                        continue
                    cp_result = cherry_pick_merge_commit(repo_path, "FETCH_HEAD")

                if cp_result.success:
                    force_push(repo_path, new_pr_branch, config.fork.remote_name)
                    console.print(f"      [green]✓[/green] Pushed [cyan]{new_pr_branch}[/cyan]")
                    state.features[feature_id] = FeatureState(
                        status="ok",
                        branch_name=new_pr_branch,
                        base_commit=onto,
                        **pr_meta,
                    )
                else:
                    console.print(f"      [red]✗[/red] Conflict!")
                    for cf in cp_result.conflict_files:
                        console.print(f"        [red]•[/red] {cf}")
                    state.features[feature_id] = FeatureState(
                        status="conflict",
                        branch_name=new_pr_branch,
                        base_commit=onto,
                        conflict_files=cp_result.conflict_files,
                        **pr_meta,
                    )

                _update_state_and_status(config, state)

    # Summary
    console.print("\n[bold]Pipeline complete.[/bold]")
    ok_count = sum(1 for fs in state.features.values() if fs.status == "ok")
    conflict_count = sum(1 for fs in state.features.values() if fs.status == "conflict")
    disabled_count = sum(1 for fs in state.features.values() if fs.status == "disabled")
    console.print(f"  Features: {ok_count} ok, {conflict_count} conflict, {disabled_count} disabled")

    return state


def _resolve_branch_target(
    config: Config, state: PipelineState, branch_name: str,
) -> tuple[str, FeatureConfig | None] | None:
    """Resolve a user-supplied branch name to the CI branch or a feature.

    Returns ("ci", None) for the CI branch, ("feature", feat) for a feature,
    or None if nothing matches.
    """
    ci_name = state.ci_branch.branch_name
    if ci_name and (branch_name == ci_name or branch_name.startswith(config.ci.branch_prefix)):
        return "ci", None

    feat = config.get_feature(branch_name) or config.get_feature_by_branch(branch_name)
    if feat is None:
        for fid, fs in state.features.items():
            if fs.branch_name == branch_name or fid == branch_name:
                feat = config.get_feature(fid)
                if feat is None:
                    # PR-sourced feature not in config — synthesize a minimal FeatureConfig
                    feat = FeatureConfig(id=fid, description=fid, source_branch="")
                break

    if feat is not None:
        return "feature", feat
    return None


def continue_branch(config: Config, branch_name: str) -> bool:
    """Mark a previously-conflicted branch as resolved after operator fixes it."""
    state = load_state(config.repo_dir)
    target = _resolve_branch_target(config, state, branch_name)

    if target is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    kind, feat = target

    if kind == "ci":
        if state.ci_branch.status != "conflict":
            console.print(
                f"[yellow]CI branch is not in conflict state "
                f"(status: {state.ci_branch.status})[/yellow]"
            )
            return False
        state.ci_branch.status = "resolved"
        state.ci_branch.conflict_files = []
        _update_state_and_status(config, state)
        console.print(
            f"[green]✓[/green] CI branch "
            f"[cyan]{state.ci_branch.branch_name}[/cyan] marked as resolved"
        )
        return True

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
    console.print(
        f"[green]✓[/green] Feature [cyan]{feat.id}[/cyan] "
        f"({fs.branch_name}) marked as resolved"
    )
    return True


def skip_branch(config: Config, branch_name: str) -> bool:
    """Mark a branch as skipped for this run."""
    state = load_state(config.repo_dir)
    target = _resolve_branch_target(config, state, branch_name)

    if target is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    kind, feat = target

    if kind == "ci":
        state.ci_branch.status = "skipped"
        state.ci_branch.conflict_files = []
        _update_state_and_status(config, state)
        console.print(
            f"[yellow]⏭[/yellow] CI branch "
            f"[cyan]{state.ci_branch.branch_name}[/cyan] skipped"
        )
        return True

    fs = state.features.get(feat.id)
    if fs is None:
        console.print(f"[red]No state found for feature {feat.id}[/red]")
        return False

    state.features[feat.id].status = "skipped"
    state.features[feat.id].conflict_files = []
    _update_state_and_status(config, state)
    console.print(f"[yellow]⏭[/yellow] Feature [cyan]{feat.id}[/cyan] skipped")
    return True


def abort_run(config: Config) -> None:
    """Abort the current run, leaving all branches as-is."""
    state = load_state(config.repo_dir)
    console.print("[yellow]Aborting current run. All branches left as-is.[/yellow]")
    _update_state_and_status(config, state)


def print_status(config: Config) -> None:
    """Print the current pipeline state to stdout."""
    from rich.table import Table

    state = load_state(config.repo_dir)

    table = Table(title="RelEasy Branch Status")
    table.add_column("Branch", style="cyan")
    table.add_column("Status")
    table.add_column("Based On")
    table.add_column("Source PR")
    table.add_column("Conflict Files", style="red")

    style_map = {
        "ok": "green", "conflict": "red", "resolved": "blue",
        "skipped": "yellow", "disabled": "dim", "pending": "dim",
    }

    def _pr_cell(fs: FeatureState | None) -> str:
        if not fs or not fs.pr_url:
            return ""
        label = f"#{fs.pr_number}" if fs.pr_number else "PR"
        return f"[link={fs.pr_url}]{label}[/link]"

    # CI branch
    ci = state.ci_branch
    ci_label = ci.branch_name or config.ci.branch_prefix
    status_style = style_map.get(ci.status, "white")
    table.add_row(
        ci_label,
        f"[{status_style}]{ci.status}[/{status_style}]",
        (ci.base_commit or "")[:12],
        "",
        ", ".join(ci.conflict_files),
    )

    # Features (source-branch and PR-based)
    shown_ids: set[str] = set()
    for feat in config.features:
        fs = state.features.get(feat.id)
        shown_ids.add(feat.id)
        if fs is None:
            status = "disabled" if not feat.enabled else "pending"
            table.add_row(feat.source_branch, f"[dim]{status}[/dim]", "", "", "")
            continue

        label = fs.branch_name or feat.source_branch
        status_style = style_map.get(fs.status, "white")
        table.add_row(
            label,
            f"[{status_style}]{fs.status}[/{status_style}]",
            (fs.base_commit or "")[:12],
            _pr_cell(fs),
            ", ".join(fs.conflict_files),
        )

    # PR features that are in state but not in config (from a previous run)
    for fid, fs in state.features.items():
        if fid in shown_ids:
            continue
        label = fs.branch_name or fid
        status_style = style_map.get(fs.status, "white")
        table.add_row(
            label,
            f"[{status_style}]{fs.status}[/{status_style}]",
            (fs.base_commit or "")[:12],
            _pr_cell(fs),
            ", ".join(fs.conflict_files),
        )

    console.print()
    console.print(f"Last run: {state.started_at or 'N/A'}  ·  Onto: {state.onto or 'N/A'}")
    console.print(table)

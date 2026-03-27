"""Phased rebase pipeline.

Phase 1 — Base + CI:
  1a. Create base branch from upstream tag/commit (e.g. antalya-26.3)
  1b. Rebase CI commits onto base → ci/antalya-26.3
  1c. Open PR: CI → base (if push enabled)
  Stop — wait for CI PR to be merged.

Phase 2 — Features (run again after CI merged):
  2a. Rebase each feature onto base → feature/antalya-26.3/<id>
  2b. Open PRs: each feature → base (if push + auto_pr enabled)

Running again resumes from the current phase stored in state.yaml.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from releasy.config import Config, FeatureConfig
from releasy.git_ops import (
    branch_exists,
    cherry_pick_merge_commit,
    commit_conflict_markers,
    count_commits,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_pr_ref,
    fetch_remote,
    find_merge_base,
    force_push,
    get_branch_tip,
    rebase_onto,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    commit_and_push_state,
    create_pull_request,
    search_prs_by_labels,
    sync_project,
)
from releasy.state import CIBranchState, FeatureState, PipelineState, load_state, save_state
from releasy.status import write_status_md

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _update_state_and_status(config: Config, state: PipelineState) -> None:
    """Persist state + STATUS.md, and optionally sync project board / push."""
    save_state(state, config.repo_dir)
    write_status_md(config, state)
    if config.push:
        sync_project(config, state)
        commit_and_push_state(f"releasy: update state — onto {state.onto}", config.repo_dir)


def _resolve_source_branch(
    state_branch: str | None,
    config_source: str,
    fork_remote: str,
    repo_path: Path | None = None,
) -> str:
    """Determine the ref to cherry-pick/rebase from.

    If a previous versioned branch exists in state, try remote then local.
    Otherwise fall back to the config's source_branch on the remote.
    """
    if state_branch and repo_path is not None:
        remote_ref = f"{fork_remote}/{state_branch}"
        result = run_git(["rev-parse", "--verify", remote_ref], repo_path, check=False)
        if result.returncode == 0:
            return remote_ref
        result = run_git(["rev-parse", "--verify", state_branch], repo_path, check=False)
        if result.returncode == 0:
            return state_branch
    elif state_branch:
        return f"{fork_remote}/{state_branch}"
    return f"{fork_remote}/{config_source}"


def _setup_repo(config: Config, work_dir: Path | None) -> Path:
    """Set up work repo and fetch remotes."""
    wd = config.resolve_work_dir(work_dir)
    console.print(f"[dim]Working directory: {wd}[/dim]")

    console.print(f"[dim]Setting up repository...[/dim]")
    repo_path = ensure_work_repo(config, wd)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    console.print(f"Fetching [cyan]{config.upstream.remote_name}[/cyan]...", end=" ")
    fetch_remote(repo_path, config.upstream.remote_name)
    console.print("[green]done[/green]")

    console.print(f"Fetching [cyan]{config.fork.remote_name}[/cyan]...", end=" ")
    fetch_remote(repo_path, config.fork.remote_name)
    console.print("[green]done[/green]")

    return repo_path


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(config: Config, onto: str, work_dir: Path | None = None) -> PipelineState:
    """Execute the phased rebase pipeline. Resumes from current state."""
    state = load_state(config.repo_dir)
    repo_path = _setup_repo(config, work_dir)

    base_branch = config.base_branch_name(onto)
    ci_branch = config.ci_branch_name(onto)
    remote = config.fork.remote_name

    # ── Phase 1a: Create base branch from upstream ──────────────────────

    if state.phase in ("init", ""):
        console.print(
            f"\n[bold]Phase 1a:[/bold] Creating base branch "
            f"[cyan]{base_branch}[/cyan] from [yellow]{onto}[/yellow]"
        )

        base_exists = branch_exists(repo_path, base_branch, remote)
        if base_exists and config.ci.if_exists == "skip":
            console.print(f"  [dim]Base branch already exists — skipping[/dim]")
        else:
            stash_and_clean(repo_path)
            create_branch_from_ref(repo_path, base_branch, onto)
            if config.push:
                force_push(repo_path, base_branch, remote)
                console.print(f"  [green]✓[/green] Pushed [cyan]{base_branch}[/cyan]")
            else:
                console.print(f"  [green]✓[/green] Created locally")

        state.set_started(onto)
        state.base_branch = base_branch
        state.phase = "base_created"
        _update_state_and_status(config, state)

    # ── Phase 1b: Rebase CI onto base (skip if no source_branch) ───────

    if state.phase == "base_created" and not config.ci.source_branch:
        console.print(
            f"\n[bold]Phase 1b:[/bold] [dim]No CI source_branch configured "
            f"— skipping CI rebase[/dim]"
        )
        state.ci_branch = CIBranchState(status="ok", base_commit=onto)
        state.phase = "ci_rebased"
        _update_state_and_status(config, state)

    if state.phase == "base_created":
        console.print(
            f"\n[bold]Phase 1b:[/bold] Rebasing CI onto "
            f"[cyan]{base_branch}[/cyan] → [cyan]{ci_branch}[/cyan]"
        )

        ci_exists = branch_exists(repo_path, ci_branch, remote)
        if ci_exists and config.ci.if_exists == "skip":
            console.print(f"  [dim]CI branch already exists — skipping[/dim]")
            state.ci_branch = CIBranchState(
                status="ok", branch_name=ci_branch, base_commit=onto,
            )
        else:
            source_ref = _resolve_source_branch(
                None, config.ci.source_branch, remote, repo_path,
            )
            onto_sha = get_branch_tip(repo_path, onto)
            divergence = find_merge_base(repo_path, source_ref, onto_sha)
            if divergence is None:
                console.print(
                    f"  [red]✗[/red] Could not find merge-base between "
                    f"{source_ref} and {onto}"
                )
                console.print(
                    f"  [dim]Hint: does '{config.ci.source_branch}' exist on the fork?[/dim]"
                )
                state.ci_branch = CIBranchState(status="conflict")
                _update_state_and_status(config, state)
                return state

            source_tip = get_branch_tip(repo_path, source_ref)
            n_commits = count_commits(repo_path, divergence, source_tip)
            console.print(f"  Source: [dim]{source_ref}[/dim] ({n_commits} commit(s))")

            stash_and_clean(repo_path)
            create_branch_from_ref(repo_path, ci_branch, base_branch)

            if n_commits > 0:
                result = rebase_onto(repo_path, base_branch, divergence, source_tip)
                if result.success:
                    run_git(["branch", "-f", ci_branch, "HEAD"], repo_path)
                    run_git(["checkout", ci_branch], repo_path)
                    applied = count_commits(repo_path, base_branch, "HEAD")
                    console.print(
                        f"  [green]✓[/green] Rebased {applied} CI commit(s) "
                        f"(from {n_commits} original, duplicates dropped)"
                    )
                else:
                    console.print(f"  [red]✗[/red] Conflict rebasing CI!")
                    for f in result.conflict_files:
                        console.print(f"    [red]•[/red] {f}")
                    console.print(
                        f"\n  [yellow]Resolve:[/yellow] cd {repo_path}"
                        f"\n  [yellow]Then:[/yellow] git add <files> && git rebase --continue"
                        f"\n  [yellow]When done:[/yellow] releasy continue --branch {ci_branch}"
                    )
                    state.ci_branch = CIBranchState(
                        status="conflict", branch_name=ci_branch,
                        base_commit=onto, conflict_files=result.conflict_files,
                    )
                    _update_state_and_status(config, state)
                    return state
            else:
                console.print(f"  [dim]No CI commits to apply[/dim]")

            if config.push:
                force_push(repo_path, ci_branch, remote)
                console.print(f"  [green]✓[/green] Pushed [cyan]{ci_branch}[/cyan]")

                ci_pr_url = create_pull_request(
                    config, ci_branch, base_branch,
                    f"[releasy] CI: {config.ci.source_branch}",
                    f"CI commits rebased from `{config.ci.source_branch}` onto `{base_branch}`.",
                )
                if ci_pr_url:
                    state.ci_branch.pr_url = ci_pr_url
                    console.print(f"  [green]✓[/green] CI PR opened: {ci_pr_url}")

            state.ci_branch = CIBranchState(
                status="ok", branch_name=ci_branch, base_commit=onto,
                pr_url=getattr(state.ci_branch, "pr_url", None),
            )

        state.phase = "ci_rebased"
        _update_state_and_status(config, state)

        console.print(
            f"\n[bold]Phase 1 complete.[/bold] CI branch: [cyan]{ci_branch}[/cyan]"
        )
        if config.push:
            console.print(
                "  Merge the CI PR, then run [bold]releasy run[/bold] again "
                "to rebase features."
            )
        else:
            console.print(
                "  Review locally, then run [bold]releasy run[/bold] again "
                "to proceed with features."
            )
        return state

    # ── Phase 2: Rebase features onto base ──────────────────────────────

    if state.phase in ("ci_rebased", "ci_merged"):
        base_branch = state.base_branch or base_branch

        console.print(
            f"\n[bold]Phase 2:[/bold] Rebasing features onto "
            f"[cyan]{base_branch}[/cyan]"
        )

        # --- PR-based features ---
        for pr_source in config.pr_sources:
            labels_str = ", ".join(pr_source.labels)
            filter_str = " (merged only)" if pr_source.merged_only else ""
            console.print(
                f"\n  Searching for PRs with labels "
                f"[yellow]{labels_str}[/yellow]{filter_str}"
            )
            prs = search_prs_by_labels(config, pr_source.labels, pr_source.merged_only)

            if not prs:
                console.print(f"    [dim]No PRs found[/dim]")
                continue

            console.print(f"    Found {len(prs)} PR(s)")
            existing_ids = {f.id for f in config.features}

            for pr in prs:
                feature_id = f"pr-{pr.number}"
                if feature_id in existing_ids:
                    continue

                new_branch = config.feature_branch_name(feature_id, onto)
                feat_exists = branch_exists(repo_path, new_branch, remote)
                if feat_exists and pr_source.if_exists == "skip":
                    console.print(
                        f"\n    [cyan]{new_branch}[/cyan] (PR #{pr.number}: {pr.title})"
                        f" — already exists, skipping"
                    )
                    continue

                desc = pr.title
                if pr_source.description:
                    desc = f"{pr_source.description}{pr.title}"

                config.features.append(FeatureConfig(
                    id=feature_id, description=desc,
                    source_branch="", enabled=True,
                ))
                existing_ids.add(feature_id)

                console.print(
                    f"\n  [cyan]{new_branch}[/cyan] (PR #{pr.number}: {pr.title})"
                )
                console.print(f"    PR: {pr.url}  [{pr.state}]")

                pr_meta = {
                    "pr_url": pr.url, "pr_number": pr.number,
                    "pr_title": pr.title, "pr_body": pr.body,
                }

                stash_and_clean(repo_path)
                create_branch_from_ref(repo_path, new_branch, base_branch)

                if pr.state == "merged" and pr.merge_commit_sha:
                    cp_result = cherry_pick_merge_commit(
                        repo_path, pr.merge_commit_sha, abort_on_conflict=False,
                    )
                else:
                    if not fetch_pr_ref(repo_path, remote, pr.number):
                        console.print(
                            f"    [red]✗[/red] Could not fetch PR #{pr.number}"
                        )
                        state.features[feature_id] = FeatureState(
                            status="conflict", branch_name=new_branch,
                            base_commit=onto, **pr_meta,
                        )
                        _update_state_and_status(config, state)
                        continue
                    cp_result = cherry_pick_merge_commit(
                        repo_path, "FETCH_HEAD", abort_on_conflict=False,
                    )

                has_conflict = not cp_result.success
                if has_conflict:
                    console.print(f"    [red]✗[/red] Conflict!")
                    for cf in cp_result.conflict_files:
                        console.print(f"      [red]•[/red] {cf}")
                    commit_conflict_markers(repo_path)
                    console.print(
                        f"    [yellow]↑[/yellow] Committed conflict markers as WIP"
                    )

                if config.push:
                    force_push(repo_path, new_branch, remote)
                    console.print(
                        f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]"
                    )
                else:
                    console.print(f"    [dim]Skipping push[/dim]")

                fs_status = "conflict" if has_conflict else "ok"
                state.features[feature_id] = FeatureState(
                    status=fs_status, branch_name=new_branch, base_commit=onto,
                    conflict_files=cp_result.conflict_files if has_conflict else [],
                    **pr_meta,
                )

                if config.push and pr_source.auto_pr:
                    pr_title = f"[releasy] {pr.title}"
                    body_parts = []
                    if has_conflict:
                        body_parts.append(
                            "> **WARNING:** Unresolved conflict markers.\n"
                        )
                    body_parts.append(f"Cherry-picked from #{pr.number}.")
                    if pr.body:
                        body_parts.append(f"\n---\n\n{pr.body}")
                    rebase_pr_url = create_pull_request(
                        config, new_branch, base_branch,
                        pr_title, "\n".join(body_parts),
                    )
                    if rebase_pr_url:
                        state.features[feature_id].rebase_pr_url = rebase_pr_url
                        console.print(
                            f"    [green]✓[/green] PR opened: {rebase_pr_url}"
                        )

                _update_state_and_status(config, state)

        # --- Static feature branches ---
        for feat in config.enabled_features:
            if not feat.source_branch:
                continue  # PR-sourced, handled above

            new_branch = config.feature_branch_name(feat.id, onto)
            console.print(
                f"\n  [cyan]{new_branch}[/cyan] ({feat.id}: {feat.description})"
            )

            source_ref = _resolve_source_branch(
                None, feat.source_branch, remote, repo_path,
            )
            ci_source_ref = _resolve_source_branch(
                None, config.ci.source_branch, remote, repo_path,
            )
            divergence = find_merge_base(repo_path, source_ref, ci_source_ref)
            if divergence is None:
                console.print(f"    [red]✗[/red] Could not find divergence")
                state.features[feat.id] = FeatureState(
                    status="conflict", branch_name=new_branch, base_commit=onto,
                )
                _update_state_and_status(config, state)
                continue

            source_tip = get_branch_tip(repo_path, source_ref)
            n_commits = count_commits(repo_path, divergence, source_tip)
            console.print(
                f"    Source: [dim]{source_ref}[/dim] ({n_commits} commit(s))"
            )

            stash_and_clean(repo_path)
            create_branch_from_ref(repo_path, new_branch, base_branch)

            if n_commits > 0:
                result = rebase_onto(
                    repo_path, base_branch, divergence, source_tip,
                )
                if result.success:
                    run_git(["branch", "-f", new_branch, "HEAD"], repo_path)
                    run_git(["checkout", new_branch], repo_path)
                    applied = count_commits(repo_path, base_branch, "HEAD")
                    console.print(
                        f"    [green]✓[/green] Rebased {applied} commit(s) "
                        f"(from {n_commits} original, duplicates dropped)"
                    )
                else:
                    console.print(f"    [red]✗[/red] Conflict!")
                    for f in result.conflict_files:
                        console.print(f"      [red]•[/red] {f}")
                    console.print(
                        f"\n    [yellow]Resolve:[/yellow] cd {repo_path}"
                        f"\n    [yellow]Then:[/yellow] git add <files> && "
                        f"git rebase --continue"
                        f"\n    [yellow]When done:[/yellow] "
                        f"releasy continue --branch {feat.id}"
                    )
                    state.features[feat.id] = FeatureState(
                        status="conflict", branch_name=new_branch,
                        base_commit=onto, conflict_files=result.conflict_files,
                    )
                    _update_state_and_status(config, state)
                    continue
            else:
                console.print(f"    [dim]No commits to apply[/dim]")

            if config.push:
                force_push(repo_path, new_branch, remote)
                console.print(
                    f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]"
                )
            else:
                console.print(f"    [dim]Skipping push[/dim]")

            state.features[feat.id] = FeatureState(
                status="ok", branch_name=new_branch, base_commit=onto,
            )

            if config.push and any(ps.auto_pr for ps in config.pr_sources):
                pr_title = f"[releasy] {feat.id}: {feat.description}"
                rebase_pr_url = create_pull_request(
                    config, new_branch, base_branch, pr_title,
                    f"Feature `{feat.id}` rebased onto `{base_branch}`.",
                )
                if rebase_pr_url:
                    state.features[feat.id].rebase_pr_url = rebase_pr_url
                    console.print(
                        f"    [green]✓[/green] PR opened: {rebase_pr_url}"
                    )

            _update_state_and_status(config, state)

        state.phase = "features_done"
        _update_state_and_status(config, state)

    # ── Summary ─────────────────────────────────────────────────────────

    console.print(f"\n[bold]Pipeline complete.[/bold] Phase: {state.phase}")
    if state.base_branch:
        console.print(f"  Base branch: [cyan]{state.base_branch}[/cyan]")
    ok = sum(1 for fs in state.features.values() if fs.status == "ok")
    conflict = sum(1 for fs in state.features.values() if fs.status == "conflict")
    if ok or conflict:
        console.print(f"  Features: {ok} ok, {conflict} conflict")

    return state


# ---------------------------------------------------------------------------
# Continue / Skip / Abort / Status
# ---------------------------------------------------------------------------


def _resolve_branch_target(
    config: Config, state: PipelineState, branch_name: str,
) -> tuple[str, FeatureConfig | None] | None:
    """Resolve a user-supplied branch name to the CI branch or a feature."""
    ci_name = state.ci_branch.branch_name
    if ci_name and (branch_name == ci_name or branch_name.startswith("ci/")):
        return "ci", None

    feat = config.get_feature(branch_name) or config.get_feature_by_branch(
        branch_name, state.onto or "",
    )
    if feat is None:
        for fid, fs in state.features.items():
            if fs.branch_name == branch_name or fid == branch_name:
                feat = config.get_feature(fid)
                if feat is None:
                    feat = FeatureConfig(id=fid, description=fid, source_branch="")
                break

    if feat is not None:
        return "feature", feat
    return None


def continue_branch(config: Config, branch_name: str) -> bool:
    """Mark a previously-conflicted branch as resolved."""
    from releasy.git_ops import is_operation_in_progress

    state = load_state(config.repo_dir)
    target = _resolve_branch_target(config, state, branch_name)

    if target is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    work_dir = config.resolve_work_dir()
    repo_path = work_dir if (work_dir / ".git").exists() else work_dir / "repo"
    if (repo_path / ".git").exists() and is_operation_in_progress(repo_path):
        console.print(
            "[red]A git operation is still in progress.[/red]\n"
            f"  cd {repo_path}\n"
            "  git add <resolved files>\n"
            "  git rebase --continue  (or git commit)\n"
            "  Then re-run this command."
        )
        return False

    kind, feat = target

    if kind == "ci":
        if state.ci_branch.status != "conflict":
            console.print(
                f"[yellow]CI branch is not in conflict "
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
            f"[yellow]Feature {feat.id} is not in conflict "
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
    """Mark a branch as skipped."""
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
    """Print the current pipeline state."""
    from rich.table import Table

    state = load_state(config.repo_dir)

    console.print()
    console.print(
        f"Last run: {state.started_at or 'N/A'}  ·  "
        f"Onto: {state.onto or 'N/A'}  ·  "
        f"Phase: {state.phase}"
    )
    if state.base_branch:
        console.print(f"Base branch: [cyan]{state.base_branch}[/cyan]")

    table = Table(title="RelEasy Branch Status")
    table.add_column("Branch", style="cyan")
    table.add_column("Status")
    table.add_column("Based On")
    table.add_column("PR")
    table.add_column("Conflict Files", style="red")

    style_map = {
        "ok": "green", "conflict": "red", "resolved": "blue",
        "skipped": "yellow", "disabled": "dim", "pending": "dim",
    }

    ci = state.ci_branch
    ci_label = ci.branch_name or "(CI)"
    s = style_map.get(ci.status, "white")
    pr_link = ""
    if ci.pr_url:
        pr_link = f"[link={ci.pr_url}]CI PR[/link]"
    table.add_row(
        ci_label, f"[{s}]{ci.status}[/{s}]",
        (ci.base_commit or "")[:12], pr_link,
        ", ".join(ci.conflict_files),
    )

    for feat in config.features:
        fs = state.features.get(feat.id)
        if fs is None:
            status = "disabled" if not feat.enabled else "pending"
            table.add_row(
                feat.source_branch or feat.id,
                f"[dim]{status}[/dim]", "", "", "",
            )
            continue
        label = fs.branch_name or feat.source_branch or feat.id
        s = style_map.get(fs.status, "white")
        pr_link = ""
        if fs.rebase_pr_url:
            pr_label = f"#{fs.pr_number}" if fs.pr_number else "PR"
            pr_link = f"[link={fs.rebase_pr_url}]{pr_label}[/link]"
        table.add_row(
            label, f"[{s}]{fs.status}[/{s}]",
            (fs.base_commit or "")[:12], pr_link,
            ", ".join(fs.conflict_files),
        )

    for fid, fs in state.features.items():
        if any(f.id == fid for f in config.features):
            continue
        label = fs.branch_name or fid
        s = style_map.get(fs.status, "white")
        pr_link = ""
        if fs.rebase_pr_url:
            pr_link = f"[link={fs.rebase_pr_url}]PR[/link]"
        table.add_row(
            label, f"[{s}]{fs.status}[/{s}]",
            (fs.base_commit or "")[:12], pr_link,
            ", ".join(fs.conflict_files),
        )

    console.print(table)

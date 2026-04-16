"""Port-cherry-pick pipeline.

Assumes the base branch (e.g. ``antalya-26.3``) already exists on origin.
For each source PR discovered via configured labels, the pipeline creates a
port branch off ``origin/<base_branch>``, cherry-picks the PR merge commit,
and on conflict either invokes the AI resolver or commits WIP markers.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from releasy.config import Config, FeatureConfig, PRSourceConfig
from releasy.git_ops import (
    branch_exists,
    cherry_pick_merge_commit,
    commit_conflict_markers,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_pr_ref,
    fetch_remote,
    force_push,
    is_operation_in_progress,
    ref_exists_locally,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    PRInfo,
    commit_and_push_state,
    create_pull_request,
    ensure_label,
    fetch_pr_by_number,
    parse_pr_url,
    search_prs_by_labels,
    sync_project,
)
from releasy.state import FeatureState, PipelineState, load_state, save_state
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


def _setup_repo(config: Config, work_dir: Path | None) -> Path:
    """Set up work repo and fetch origin. Upstream is not fetched anymore —
    the ``onto`` argument is used only for branch naming."""
    wd = config.resolve_work_dir(work_dir)
    console.print(f"[dim]Working directory: {wd}[/dim]")

    console.print("[dim]Setting up repository...[/dim]")
    repo_path = ensure_work_repo(config, wd)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    console.print(f"Fetching [cyan]{config.origin.remote_name}[/cyan]...", end=" ")
    fetch_remote(repo_path, config.origin.remote_name)
    console.print("[green]done[/green]")

    return repo_path


def _push(config: Config, repo_path: Path, branch: str) -> None:
    """Push a branch to the origin remote, with upstream safety check."""
    force_push(
        repo_path, branch, config.origin.remote_name,
        upstream_name=config.upstream.remote_name if config.upstream else None,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    config: Config,
    onto: str,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
) -> PipelineState:
    """Port PRs onto ``origin/<base_branch>``.

    ``resolve_conflicts`` is a CLI-level kill-switch. The AI resolver only
    runs when both this flag and ``config.ai_resolve.enabled`` are true.
    """
    state = load_state(config.repo_dir)
    repo_path = _setup_repo(config, work_dir)

    base_branch = config.base_branch_name(onto)
    remote = config.origin.remote_name

    # Verify the base branch already exists on origin.
    if not branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] does not exist "
            f"on remote [cyan]{remote}[/cyan].\n"
            f"  Create and push it first, then re-run."
        )
        raise SystemExit(2)

    base_ref = f"{remote}/{base_branch}"
    console.print(f"Base: [cyan]{base_ref}[/cyan]")

    state.set_started(onto)
    state.base_branch = base_branch
    state.phase = "init"
    _update_state_and_status(config, state)

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if ai_active:
        console.print(
            f"[dim]AI conflict resolver: enabled "
            f"(command='{config.ai_resolve.command}', "
            f"label='{config.ai_resolve.label}', "
            f"max_iterations={config.ai_resolve.max_iterations})[/dim]"
        )
        if config.push:
            ensure_label(
                config,
                config.ai_resolve.label,
                config.ai_resolve.label_color,
                "Port conflict auto-resolved by Claude",
            )
    else:
        why = "disabled via --no-resolve-conflicts" if not resolve_conflicts else "disabled in config"
        console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    console.print(
        f"\n[bold]Phase:[/bold] Porting PRs onto [cyan]{base_branch}[/cyan]"
    )

    # --- Collect PRs from all sources (union) ---
    collected: dict[int, tuple[PRInfo, PRSourceConfig]] = {}
    for pr_source in config.pr_sources.by_labels:
        labels_str = ", ".join(pr_source.labels)
        filter_str = " (merged only)" if pr_source.merged_only else ""
        console.print(
            f"\n  Searching for PRs with labels "
            f"[yellow]{labels_str}[/yellow]{filter_str}"
        )
        prs = search_prs_by_labels(config, pr_source.labels, pr_source.merged_only)

        if not prs:
            console.print("    [dim]No PRs found[/dim]")
            continue

        console.print(f"    Found {len(prs)} PR(s)")
        for pr in prs:
            if pr.number not in collected:
                collected[pr.number] = (pr, pr_source)

    prs_cfg = config.pr_sources
    include_pr_numbers = {
        n for url in prs_cfg.include_prs if (n := parse_pr_url(url)) is not None
    }
    exclude_pr_numbers = {
        n for url in prs_cfg.exclude_prs if (n := parse_pr_url(url)) is not None
    }

    if prs_cfg.exclude_labels:
        exclude_set = set(prs_cfg.exclude_labels)
        before = len(collected)
        collected = {
            num: (pr, src)
            for num, (pr, src) in collected.items()
            if not (set(pr.labels) & exclude_set) or num in include_pr_numbers
        }
        removed = before - len(collected)
        if removed:
            console.print(
                f"\n  [dim]Excluded {removed} PR(s) by label filter "
                f"({', '.join(prs_cfg.exclude_labels)})[/dim]"
            )

    if include_pr_numbers:
        default_source = (
            config.pr_sources.by_labels[0]
            if config.pr_sources.by_labels
            else PRSourceConfig(labels=[])
        )
        for pr_num in sorted(include_pr_numbers):
            if pr_num in collected:
                continue
            console.print(f"\n  Fetching explicitly included PR #{pr_num}...")
            pr_info = fetch_pr_by_number(config, pr_num)
            if pr_info:
                collected[pr_num] = (pr_info, default_source)
                console.print(f"    [green]✓[/green] {pr_info.title}")
            else:
                console.print(f"    [red]✗[/red] Could not fetch PR #{pr_num}")

    for pr_num in exclude_pr_numbers:
        if pr_num in collected:
            pr_info, _ = collected.pop(pr_num)
            console.print(f"\n  [dim]Excluded PR #{pr_num} ({pr_info.title})[/dim]")

    sorted_prs = sorted(
        collected.values(),
        key=lambda pair: (pair[0].merged_at or "9999", pair[0].number),
    )

    if not sorted_prs:
        console.print("\n  [dim]No PRs to process after filtering[/dim]")

    existing_ids = {f.id for f in config.features}
    for pr, pr_source in sorted_prs:
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
        create_branch_from_ref(repo_path, new_branch, base_ref)

        if pr.state == "merged" and pr.merge_commit_sha:
            cp_result = cherry_pick_merge_commit(
                repo_path, pr.merge_commit_sha, abort_on_conflict=False,
            )
        else:
            if not fetch_pr_ref(repo_path, remote, pr.number):
                console.print(f"    [red]✗[/red] Could not fetch PR #{pr.number}")
                state.features[feature_id] = FeatureState(
                    status="conflict", branch_name=new_branch,
                    base_commit=onto, **pr_meta,
                )
                _update_state_and_status(config, state)
                continue
            cp_result = cherry_pick_merge_commit(
                repo_path, "FETCH_HEAD", abort_on_conflict=False,
            )

        if cp_result.success:
            _finish_clean_port(
                config, repo_path, state, feature_id, new_branch,
                base_branch, onto, pr, pr_source, pr_meta,
            )
            continue

        # --- Conflict path ---
        console.print("    [red]✗[/red] Conflict!")
        for cf in cp_result.conflict_files:
            console.print(f"      [red]•[/red] {cf}")

        handled = False
        if ai_active:
            handled = _try_ai_resolve(
                config, repo_path, state, feature_id, new_branch,
                base_branch, onto, pr, cp_result.conflict_files, pr_meta,
            )

        if not handled:
            # Fallback: commit markers as WIP, push, flag conflict.
            commit_conflict_markers(repo_path)
            console.print("    [yellow]↑[/yellow] Committed conflict markers as WIP")

            if config.push:
                _push(config, repo_path, new_branch)
                console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
            else:
                console.print("    [dim]Skipping push[/dim]")

            state.features[feature_id] = FeatureState(
                status="conflict", branch_name=new_branch, base_commit=onto,
                conflict_files=cp_result.conflict_files, **pr_meta,
            )

            if config.push and pr_source.auto_pr:
                body_parts = [
                    "> **WARNING:** Unresolved conflict markers.\n",
                    f"Cherry-picked from #{pr.number}.",
                ]
                if pr.body:
                    body_parts.append(f"\n---\n\n{pr.body}")
                rebase_pr_url = create_pull_request(
                    config, new_branch, base_branch,
                    f"[releasy] {pr.title}", "\n".join(body_parts),
                )
                if rebase_pr_url:
                    state.features[feature_id].rebase_pr_url = rebase_pr_url
                    console.print(f"    [green]✓[/green] PR opened: {rebase_pr_url}")

            _update_state_and_status(config, state)

    state.phase = "ports_done"
    _update_state_and_status(config, state)

    # --- Summary ---
    console.print(f"\n[bold]Pipeline complete.[/bold] Phase: {state.phase}")
    if state.base_branch:
        console.print(f"  Base branch: [cyan]{state.base_branch}[/cyan]")
    ok = sum(1 for fs in state.features.values() if fs.status == "ok")
    resolved = sum(
        1 for fs in state.features.values()
        if fs.status == "resolved" and fs.ai_resolved
    )
    conflict = sum(1 for fs in state.features.values() if fs.status == "conflict")
    if ok or resolved or conflict:
        console.print(
            f"  Ports: {ok} ok, {resolved} ai-resolved, {conflict} conflict"
        )

    return state


def _finish_clean_port(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    feature_id: str,
    new_branch: str,
    base_branch: str,
    onto: str,
    pr: PRInfo,
    pr_source: PRSourceConfig,
    pr_meta: dict,
) -> None:
    """Push and (optionally) open a PR for a conflict-free port."""
    if config.push:
        _push(config, repo_path, new_branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
    else:
        console.print("    [dim]Skipping push[/dim]")

    state.features[feature_id] = FeatureState(
        status="ok", branch_name=new_branch, base_commit=onto, **pr_meta,
    )

    if config.push and pr_source.auto_pr:
        body_parts = [f"Cherry-picked from #{pr.number}."]
        if pr.body:
            body_parts.append(f"\n---\n\n{pr.body}")
        rebase_pr_url = create_pull_request(
            config, new_branch, base_branch,
            f"[releasy] {pr.title}", "\n".join(body_parts),
        )
        if rebase_pr_url:
            state.features[feature_id].rebase_pr_url = rebase_pr_url
            console.print(f"    [green]✓[/green] PR opened: {rebase_pr_url}")

    _update_state_and_status(config, state)


def _try_ai_resolve(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    feature_id: str,
    new_branch: str,
    base_branch: str,
    onto: str,
    pr: PRInfo,
    conflict_files: list[str],
    pr_meta: dict,
) -> bool:
    """Invoke Claude to resolve, build, commit, push, and open the PR.

    Returns True when the AI resolver produced a pushed branch + PR with the
    AI label. On any failure the caller falls back to the WIP-marker path.
    """
    from releasy.ai_resolve import AIResolveContext, resolve_with_claude

    ctx = AIResolveContext(
        port_branch=new_branch,
        base_branch=base_branch,
        source_pr=pr,
        conflict_files=conflict_files,
    )

    result = resolve_with_claude(config, repo_path, ctx)

    if not result.success:
        reason = result.error or ("timed out" if result.timed_out else "unknown failure")
        console.print(f"    [yellow]AI resolve failed:[/yellow] {reason}")
        # Leave working tree as the resolver left it so the human can inspect,
        # but we still want a clean slate to commit the WIP fallback. If the
        # cherry-pick is still in progress, abort it first.
        if is_operation_in_progress(repo_path):
            run_git(["cherry-pick", "--abort"], repo_path, check=False)
            run_git(["merge", "--abort"], repo_path, check=False)
            run_git(["rebase", "--abort"], repo_path, check=False)
            # Re-apply the conflict so markers can be committed.
            if pr.merge_commit_sha:
                cherry_pick_merge_commit(
                    repo_path, pr.merge_commit_sha, abort_on_conflict=False,
                )
            else:
                cherry_pick_merge_commit(
                    repo_path, "FETCH_HEAD", abort_on_conflict=False,
                )
        return False

    console.print(
        f"    [green]✓[/green] AI resolved (iterations: {result.iterations}) "
        f"→ {result.pr_url or '(no PR URL)'}"
    )
    state.features[feature_id] = FeatureState(
        status="resolved",
        branch_name=new_branch,
        base_commit=onto,
        conflict_files=[],
        rebase_pr_url=result.pr_url,
        ai_resolved=True,
        ai_iterations=result.iterations,
        **pr_meta,
    )
    _update_state_and_status(config, state)
    return True


# ---------------------------------------------------------------------------
# Continue / Skip / Abort / Status
# ---------------------------------------------------------------------------


def _resolve_branch_target(
    config: Config, state: PipelineState, branch_name: str,
) -> FeatureConfig | None:
    """Resolve a user-supplied branch name or feature ID."""
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
    return feat


def continue_branch(config: Config, branch_name: str) -> bool:
    """Mark a previously-conflicted port as resolved."""
    state = load_state(config.repo_dir)
    feat = _resolve_branch_target(config, state, branch_name)

    if feat is None:
        console.print(f"[red]Unknown branch or feature: {branch_name}[/red]")
        return False

    work_dir = config.resolve_work_dir()
    repo_path = work_dir if (work_dir / ".git").exists() else work_dir / "repo"
    if (repo_path / ".git").exists() and is_operation_in_progress(repo_path):
        console.print(
            "[red]A git operation is still in progress.[/red]\n"
            f"  cd {repo_path}\n"
            "  git add <resolved files>\n"
            "  git cherry-pick --continue  (or git commit)\n"
            "  Then re-run this command."
        )
        return False

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
    """Mark a port branch as skipped."""
    state = load_state(config.repo_dir)
    feat = _resolve_branch_target(config, state, branch_name)

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

    table = Table(title="RelEasy Port Status")
    table.add_column("Branch", style="cyan")
    table.add_column("Status")
    table.add_column("AI", style="magenta")
    table.add_column("Based On")
    table.add_column("PR")
    table.add_column("Conflict Files", style="red")

    style_map = {
        "ok": "green", "conflict": "red", "resolved": "blue",
        "skipped": "yellow", "disabled": "dim", "pending": "dim",
    }

    def _ai_cell(fs: FeatureState) -> str:
        if not fs.ai_resolved:
            return ""
        iters = f" ({fs.ai_iterations}×)" if fs.ai_iterations else ""
        return f"[magenta]ai-resolved[/magenta]{iters}"

    for feat in config.features:
        fs = state.features.get(feat.id)
        if fs is None:
            status = "disabled" if not feat.enabled else "pending"
            table.add_row(
                feat.source_branch or feat.id,
                f"[dim]{status}[/dim]", "", "", "", "",
            )
            continue
        label = fs.branch_name or feat.source_branch or feat.id
        s = style_map.get(fs.status, "white")
        pr_link = ""
        if fs.rebase_pr_url:
            pr_label = f"#{fs.pr_number}" if fs.pr_number else "PR"
            pr_link = f"[link={fs.rebase_pr_url}]{pr_label}[/link]"
        table.add_row(
            label, f"[{s}]{fs.status}[/{s}]", _ai_cell(fs),
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
            label, f"[{s}]{fs.status}[/{s}]", _ai_cell(fs),
            (fs.base_commit or "")[:12], pr_link,
            ", ".join(fs.conflict_files),
        )

    console.print(table)

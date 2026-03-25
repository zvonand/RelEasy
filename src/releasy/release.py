"""Release branch construction — PR-per-feature workflow.

Creates a base release branch (upstream tag + CI commits), then for each
enabled feature creates a separate branch with a single squashed commit
and opens a GitHub PR targeting the release branch.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from rich.console import Console

from releasy.config import Config, FeatureConfig
from releasy.git_ops import (
    get_conflict_files,
    run_git,
    cherry_pick_range,
    count_commits,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_all,
    force_push,
    get_branch_tip,
    resolve_ref,
    squash_commits,
    stash_and_clean,
)
from releasy.github_ops import create_pull_request
from releasy.state import load_state

console = Console()


def _feature_pr_branch(release_name: str, feature_id: str) -> str:
    """Naming convention for per-feature PR branches."""
    return f"{release_name}/feat/{feature_id}"


def _build_pr_body(
    feat: FeatureConfig,
    n_commits: int,
    release_name: str,
    original_pr_body: str | None = None,
) -> str:
    """Build the PR body for a feature."""
    lines = [
        f"## {feat.description}",
        "",
        f"Feature `{feat.id}` squashed into a single commit ({n_commits} original commit(s)).",
        "",
    ]

    if original_pr_body:
        lines.append("### Original PR Description")
        lines.append("")
        lines.append(original_pr_body)
        lines.append("")

    if feat.depends_on:
        dep_links = []
        for dep_id in feat.depends_on:
            dep_branch = _feature_pr_branch(release_name, dep_id)
            dep_links.append(f"`{dep_branch}`")
        lines.append(f"**Merge after:** {', '.join(dep_links)}")
        lines.append("")

    lines.append("---")
    lines.append("*Created automatically by RelEasy.*")
    return "\n".join(lines)


def build_release(
    config: Config,
    upstream_tag: str,
    branch_name: str,
    strict: bool = False,
    include_skipped: bool = False,
    work_dir: Path | None = None,
) -> bool:
    """Build a release with PR-per-feature workflow.

    Steps:
    1. Pre-flight checks on feature status
    2. Create release base branch from upstream tag + CI commits
    3. Push the base branch
    4. For each feature: create a branch with squashed commit, push, open PR
    """
    state = load_state()

    # Synthesize FeatureConfig for PR-sourced features found in state
    existing_ids = {f.id for f in config.features}
    for fid, fs in state.features.items():
        if fs.pr_number and fid not in existing_ids:
            config.features.append(FeatureConfig(
                id=fid,
                description=fs.pr_title or f"PR #{fs.pr_number}",
                source_branch="",
                enabled=True,
            ))

    # --- Pre-flight checks ---
    warnings = []
    for feat in config.enabled_features:
        fs = state.features.get(feat.id)
        if fs is None:
            warnings.append(f"  {feat.id}: no state (never run)")
            continue
        if fs.status == "conflict":
            warnings.append(f"  {feat.id}: [red]conflict[/red]")
        elif fs.status == "skipped" and not include_skipped:
            warnings.append(
                f"  {feat.id}: [yellow]skipped[/yellow] (use --include-skipped to include)"
            )

    if warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(w)
        if strict:
            console.print("\n[red]Aborting due to --strict flag.[/red]")
            return False
        console.print()

    features_to_include: list[FeatureConfig] = []
    for feat in config.enabled_features:
        fs = state.features.get(feat.id)
        if fs is None:
            continue
        if fs.status == "conflict":
            console.print(f"[yellow]Skipping {feat.id} (conflict)[/yellow]")
            continue
        if fs.status == "skipped" and not include_skipped:
            console.print(f"[yellow]Skipping {feat.id} (skipped)[/yellow]")
            continue
        features_to_include.append(feat)

    # --- Set up working repo ---
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="releasy-release-"))
    console.print(f"[dim]Working directory: {work_dir}[/dim]")

    repo_path = ensure_work_repo(config, work_dir)
    fetch_all(config, repo_path)

    # --- Step 1: Create release base branch from upstream tag ---
    console.print(
        f"\n[bold]Creating release branch[/bold] [cyan]{branch_name}[/cyan] "
        f"from tag [yellow]{upstream_tag}[/yellow]"
    )

    tag_sha = resolve_ref(repo_path, upstream_tag)
    if tag_sha is None:
        console.print(f"[red]Tag {upstream_tag} not found[/red]")
        return False

    create_branch_from_ref(repo_path, branch_name, tag_sha)
    console.print(f"  [green]✓[/green] Branch created at {tag_sha[:12]}")

    # --- Step 2: Apply CI commits to the base branch ---
    ci_state = state.ci_branch
    if ci_state.branch_name and ci_state.base_commit:
        console.print(
            f"\n[bold]Applying CI commits[/bold] from [cyan]{ci_state.branch_name}[/cyan]"
        )
        ci_tip = get_branch_tip(
            repo_path, f"{config.fork.remote_name}/{ci_state.branch_name}"
        )
        n_ci = count_commits(repo_path, ci_state.base_commit, ci_tip)
        if n_ci > 0:
            result = cherry_pick_range(repo_path, ci_state.base_commit, ci_tip)
            if not result.success:
                console.print(f"  [red]✗[/red] Conflict applying CI commits!")
                for f in result.conflict_files:
                    console.print(f"    [red]•[/red] {f}")
                return False
            console.print(f"  [green]✓[/green] Applied {n_ci} CI commit(s)")
        else:
            console.print(f"  [dim]No CI commits to apply[/dim]")
    else:
        console.print(f"\n[dim]No CI branch state — skipping CI commits[/dim]")

    # --- Step 3: Push the base release branch ---
    console.print(f"\n[bold]Pushing base branch[/bold] [cyan]{branch_name}[/cyan]")
    force_push(repo_path, branch_name, config.fork.remote_name)
    console.print(f"  [green]✓[/green] Pushed")

    # --- Step 4: Per-feature branches and PRs ---
    console.print(f"\n[bold]Creating per-feature branches and PRs[/bold]")

    pr_results: list[tuple[str, str, str | None]] = []  # (feature_id, branch, pr_url)

    for feat in features_to_include:
        fs = state.features.get(feat.id)
        if not fs or not fs.branch_name or not fs.base_commit:
            console.print(f"\n  [yellow]Skipping {feat.id} — no branch state[/yellow]")
            continue

        feat_pr_branch = _feature_pr_branch(branch_name, feat.id)
        console.print(f"\n  [cyan]{feat_pr_branch}[/cyan] ({feat.id}: {feat.description})")

        feat_remote_ref = f"{config.fork.remote_name}/{fs.branch_name}"
        feat_tip = get_branch_tip(repo_path, feat_remote_ref)

        # Feature-only commits: everything on the feature branch after the CI branch tip
        ci_at_feat_time = get_branch_tip(
            repo_path,
            f"{config.fork.remote_name}/{ci_state.branch_name}"
            if ci_state.branch_name
            else fs.base_commit,
        )

        n_feat = count_commits(repo_path, ci_at_feat_time, feat_tip)
        if n_feat == 0:
            console.print(f"    [dim]No unique commits — skipping[/dim]")
            continue

        # Create the PR branch from the release base
        stash_and_clean(repo_path)
        create_branch_from_ref(repo_path, feat_pr_branch, branch_name)

        # Squash feature commits on a temp branch, then cherry-pick the single commit
        tmp = f"_releasy_squash_{feat.id}"
        run_git(["checkout", "-b", tmp, feat_tip], repo_path, check=False)

        squash_result = squash_commits(
            repo_path,
            ci_at_feat_time,
            f"[releasy] {feat.id}: {feat.description}",
        )

        if not squash_result.success:
            console.print(
                f"    [red]✗[/red] Failed to squash: {squash_result.error_message}"
            )
            stash_and_clean(repo_path)
            run_git(["checkout", branch_name], repo_path)
            run_git(["branch", "-D", tmp], repo_path, check=False)
            run_git(["branch", "-D", feat_pr_branch], repo_path, check=False)
            continue

        squashed_sha = get_branch_tip(repo_path, "HEAD")

        # Cherry-pick the squashed commit onto the PR branch
        run_git(["checkout", feat_pr_branch], repo_path)
        cp = run_git(["cherry-pick", squashed_sha], repo_path, check=False)
        run_git(["branch", "-D", tmp], repo_path, check=False)

        if cp.returncode != 0:
            console.print(f"    [red]✗[/red] Conflict applying squashed feature!")
            for f in get_conflict_files(repo_path):
                console.print(f"      [red]•[/red] {f}")
            run_git(["cherry-pick", "--abort"], repo_path, check=False)
            stash_and_clean(repo_path)
            run_git(["checkout", branch_name], repo_path)
            run_git(["branch", "-D", feat_pr_branch], repo_path, check=False)
            continue

        # Push the feature PR branch
        force_push(repo_path, feat_pr_branch, config.fork.remote_name)
        console.print(f"    [green]✓[/green] Pushed ({n_feat} commits squashed into 1)")

        # Open a PR — reuse original PR title/body if the feature came from a PR
        original_body = fs.pr_body if fs.pr_body else None
        pr_title = f"[releasy] {feat.id}: {feat.description}"
        pr_body = _build_pr_body(feat, n_feat, branch_name, original_body)
        pr_url = create_pull_request(config, feat_pr_branch, branch_name, pr_title, pr_body)

        if pr_url:
            console.print(f"    [green]✓[/green] PR opened: {pr_url}")
        else:
            console.print(
                f"    [yellow]![/yellow] Branch pushed but PR not created "
                f"(set RELEASY_GITHUB_TOKEN to enable)"
            )

        pr_results.append((feat.id, feat_pr_branch, pr_url))

        # Return to base branch for the next iteration
        stash_and_clean(repo_path)
        run_git(["checkout", branch_name], repo_path)

    # --- Summary ---
    console.print(f"\n[bold]Release summary for [cyan]{branch_name}[/cyan][/bold]")
    console.print(f"  Base: {upstream_tag} + CI commits")

    if pr_results:
        console.print(f"  Feature PRs:")
        for feat_id, pr_branch, pr_url in pr_results:
            if pr_url:
                console.print(f"    [green]✓[/green] {feat_id}: {pr_url}")
            else:
                console.print(f"    [yellow]![/yellow] {feat_id}: {pr_branch} (no PR created)")

        has_deps = any(
            f.depends_on
            for fid, _, _ in pr_results
            if (f := config.get_feature(fid))
        )
        if has_deps:
            console.print(
                "\n  [yellow]Note:[/yellow] Some features have dependencies — "
                "merge PRs in the order listed."
            )
    else:
        console.print(f"  [dim]No feature PRs created[/dim]")

    return True

"""Release branch construction — squash features onto an upstream tag."""

from __future__ import annotations

import tempfile
from pathlib import Path

from rich.console import Console

from releasy.config import Config
from releasy.git_ops import (
    checkout_branch,
    cherry_pick_range,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_all,
    force_push,
    get_branch_tip,
    get_commit_range,
    rebase_onto,
    stash_and_clean,
)
from releasy.state import load_state

console = Console()


def build_release(
    config: Config,
    upstream_tag: str,
    branch_name: str,
    strict: bool = False,
    include_skipped: bool = False,
    work_dir: Path | None = None,
) -> bool:
    """Build a release branch from an upstream tag with CI + feature commits.

    Steps:
    1. Warn about non-ok features
    2. Create branch from upstream tag
    3. Cherry-pick CI branch commits onto release
    4. Squash-apply each enabled feature branch
    5. Push the release branch
    """
    state = load_state()

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
            warnings.append(f"  {feat.id}: [yellow]skipped[/yellow] (use --include-skipped to include)")

    if warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in warnings:
            console.print(w)
        if strict:
            console.print("\n[red]Aborting due to --strict flag.[/red]")
            return False
        console.print()

    # Determine which features to include
    features_to_include = []
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

    # --- Step 2: Create release branch from upstream tag ---
    console.print(f"\n[bold]Creating release branch[/bold] [cyan]{branch_name}[/cyan] from tag [yellow]{upstream_tag}[/yellow]")

    tag_ref = f"{config.upstream.remote_name}/{upstream_tag}" if "/" not in upstream_tag else upstream_tag
    # Try direct tag ref first, then with remote prefix
    from releasy.git_ops import _run_git
    result = _run_git(["rev-parse", upstream_tag], repo_path, check=False)
    if result.returncode == 0:
        tag_ref = upstream_tag
    else:
        result = _run_git(["rev-parse", f"refs/tags/{upstream_tag}"], repo_path, check=False)
        if result.returncode == 0:
            tag_ref = f"refs/tags/{upstream_tag}"
        else:
            console.print(f"[red]Tag {upstream_tag} not found[/red]")
            return False

    create_branch_from_ref(repo_path, branch_name, tag_ref)
    console.print(f"  [green]✓[/green] Branch created")

    # --- Step 3: Cherry-pick CI branch commits ---
    console.print(f"\n[bold]Applying CI branch[/bold] [cyan]{config.ci_branch}[/cyan] commits")

    # Find the divergence point between ci_branch and the upstream tag
    ci_tip = get_branch_tip(repo_path, f"{config.fork.remote_name}/{config.ci_branch}")
    tag_sha = get_branch_tip(repo_path, tag_ref)

    # Get commits unique to CI branch (those after it diverges from upstream)
    ci_commits = get_commit_range(repo_path, tag_sha, ci_tip)

    if ci_commits:
        result = cherry_pick_range(repo_path, tag_sha, ci_tip)
        if not result.success:
            console.print(f"  [red]✗[/red] Conflict applying CI commits!")
            for f in result.conflict_files:
                console.print(f"    [red]•[/red] {f}")
            return False
        console.print(f"  [green]✓[/green] Applied {len(ci_commits)} CI commit(s)")
    else:
        console.print(f"  [dim]No CI commits to apply[/dim]")

    # --- Step 4: Squash-apply each feature branch ---
    for feat in features_to_include:
        console.print(f"\n[bold]Applying feature[/bold] [cyan]{feat.id}[/cyan]: {feat.description}")

        # Save current position on release branch
        release_tip = get_branch_tip(repo_path, "HEAD")

        # Get the feature branch commits relative to CI branch
        feat_tip = get_branch_tip(repo_path, f"{config.fork.remote_name}/{feat.branch}")
        ci_branch_tip = get_branch_tip(repo_path, f"{config.fork.remote_name}/{config.ci_branch}")

        feat_commits = get_commit_range(repo_path, ci_branch_tip, feat_tip)
        if not feat_commits:
            console.print(f"  [dim]No unique commits in {feat.branch}[/dim]")
            continue

        # Create a temporary branch to squash the feature
        stash_and_clean(repo_path)
        _run_git(["checkout", "-b", f"_releasy_squash_{feat.id}", feat_tip], repo_path, check=False)

        # Squash all feature commits into one
        from releasy.git_ops import squash_commits
        squash_result = squash_commits(
            repo_path,
            ci_branch_tip,
            f"[releasy] {feat.id}: {feat.description}",
        )

        if not squash_result.success:
            console.print(f"  [red]✗[/red] Failed to squash feature commits: {squash_result.error_message}")
            stash_and_clean(repo_path)
            _run_git(["checkout", branch_name], repo_path)
            _run_git(["branch", "-D", f"_releasy_squash_{feat.id}"], repo_path, check=False)
            return False

        squashed_sha = get_branch_tip(repo_path, "HEAD")

        # Switch back to release branch and cherry-pick the squashed commit
        _run_git(["checkout", branch_name], repo_path)
        cp_result = _run_git(["cherry-pick", squashed_sha], repo_path, check=False)

        # Clean up temp branch
        _run_git(["branch", "-D", f"_releasy_squash_{feat.id}"], repo_path, check=False)

        if cp_result.returncode != 0:
            console.print(f"  [red]✗[/red] Conflict applying squashed feature!")
            from releasy.git_ops import _get_conflict_files
            conflicts = _get_conflict_files(repo_path)
            for f in conflicts:
                console.print(f"    [red]•[/red] {f}")
            _run_git(["cherry-pick", "--abort"], repo_path, check=False)
            return False

        console.print(f"  [green]✓[/green] Applied ({len(feat_commits)} commits squashed)")

    # --- Step 5: Push release branch ---
    console.print(f"\n[bold]Pushing release branch[/bold] [cyan]{branch_name}[/cyan]")
    force_push(repo_path, branch_name, config.fork.remote_name)
    console.print(f"[green]✓[/green] Release branch [cyan]{branch_name}[/cyan] pushed successfully!")

    return True

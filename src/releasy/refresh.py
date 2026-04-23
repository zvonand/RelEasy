"""``releasy refresh`` — keep already-tracked rebase PRs current.

This is a strict maintenance loop. It does **not** create new branches,
discover new source PRs, or open new pull requests. It only walks the
PRs RelEasy is already tracking in its state file and tries to refresh
each one against the latest target branch.

After ``releasy run`` opens a batch of rebase PRs, the target branch
(``origin/<base_branch>``) keeps moving as other work lands. Some PRs
will eventually conflict with the new tip even though they were clean
when first opened. That's what this command exists for:

1. Iterate every tracked PR (entries in ``state.features`` that have a
   ``rebase_pr_url`` and a live ``branch_name``). No discovery, no new
   entries, no new PRs.
2. Fetch the latest tips of the target branch and the PR branch from
   origin (so we don't operate on a stale local copy).
3. Attempt ``git merge --no-ff origin/<base_branch>`` into the PR branch.
   - Clean merge → leave the PR alone (we only act on conflicts).
   - Conflict → invoke the same AI resolver used for cherry-picks (with
     the merge-flavoured prompt) and, on success, push the resolved
     merge commit to the PR branch (status preserved).
   - AI gives up / disabled → abort the merge, reset the local branch
     back to its original tip, and mark the entry as ``conflict`` in
     state + project board.

The AI resolver invocation is shared with the cherry-pick path
(``ai_resolve.attempt_ai_resolve``) so prompt rendering, claude
subprocess management, postcondition verification, and worktree cleanup
all behave identically; only the prompt template (``merge_prompt_file``)
and the in-progress operation differ.
"""

from __future__ import annotations

from pathlib import Path

from releasy.termlog import console

from releasy.ai_resolve import AIResolveContext, attempt_ai_resolve
from releasy.config import Config
from releasy.git_ops import (
    get_conflict_files,
    is_operation_in_progress,
    remote_branch_exists,
    run_git,
    stash_and_clean,
)
from releasy.github_ops import (
    PRInfo,
    parse_pr_url,
    sync_project,
)
from releasy.state import FeatureState, PipelineState, load_state, save_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist(config: Config, state: PipelineState) -> None:
    """Mirror ``pipeline._persist_state`` — kept local to avoid a circular
    import (``pipeline`` itself imports nothing from here).

    Always writes the per-project state file. When ``push`` is on we
    also reconcile the GitHub Project board so the project view reflects
    the new ``conflict`` status as soon as we mark it (instead of
    waiting for the next ``releasy continue`` pass).
    """
    save_state(state, config)
    if config.push:
        sync_project(config, state)


def _synthesise_source_pr(fs: FeatureState) -> PRInfo | None:
    """Build a ``PRInfo`` from cached FeatureState fields.

    Used to feed the AI resolver context without re-fetching from
    GitHub. We have everything the prompt needs (URL, title, body,
    number, repo slug parsed from the URL); the head SHA / merge SHA
    are irrelevant for merge-conflict resolution so we leave them
    blank.
    """
    if not fs.pr_url:
        return None
    parsed = parse_pr_url(fs.pr_url)
    if not parsed:
        return None
    owner, repo, num = parsed
    return PRInfo(
        number=fs.pr_number or num,
        title=fs.pr_title or "",
        body=fs.pr_body or "",
        state="merged",
        merge_commit_sha=None,
        head_sha="",
        url=fs.pr_url,
        repo_slug=f"{owner}/{repo}",
    )


def _refresh_local_branch(
    repo_path: Path, branch: str, remote: str,
) -> str | None:
    """Force-checkout ``branch`` to match ``origin/<branch>``.

    Returns the resulting HEAD SHA, or ``None`` if the checkout failed
    (caller logs and skips the entry). Stashes / cleans any leftover
    state in the worktree first so we never start a merge on top of
    half-resolved files from a previous run.
    """
    stash_and_clean(repo_path)
    co = run_git(
        ["checkout", "-B", branch, f"{remote}/{branch}"],
        repo_path, check=False,
    )
    if co.returncode != 0:
        return None
    head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    if head.returncode != 0:
        return None
    return head.stdout.strip()


def _abort_any_merge(repo_path: Path, fallback_sha: str | None) -> None:
    """Best-effort cleanup after a failed / unwanted merge.

    Aborts whatever git op is in progress, then hard-resets to
    ``fallback_sha`` if provided so we never leave the working tree on
    an unintended merge commit.
    """
    if is_operation_in_progress(repo_path):
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)
    if fallback_sha:
        run_git(["reset", "--hard", fallback_sha], repo_path, check=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def refresh_tracked_prs(
    config: Config,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
) -> bool:
    """Walk tracked PRs, merge target into each, AI-resolve any conflicts.

    Strictly a maintenance / refresh pass: never opens new PRs, never
    creates new branches, never discovers new PR sources. Only
    operates on entries already present in ``state.features``.

    See module docstring for the detailed contract. Returns False when
    one or more PRs ended up in (or stayed in) ``conflict`` status — so
    the CLI can exit non-zero for shell scripts.
    """
    # Late import to avoid a cycle: pipeline imports nothing from here,
    # but it owns the shared repo-setup helper we want to reuse.
    from releasy.pipeline import _setup_repo

    state = load_state(config)
    if not state.features:
        console.print(
            "[yellow]No features in state. Run 'releasy run' first.[/yellow]"
        )
        return True

    base_branch = state.base_branch or (
        config.base_branch_name(state.onto or "") if state.onto else None
    )
    if not base_branch:
        console.print(
            "[red]Cannot determine base branch from state.[/red] "
            "Run 'releasy run' first."
        )
        return False

    repo_path = _setup_repo(config, work_dir, base_branch)

    if is_operation_in_progress(repo_path):
        console.print(
            f"\n[red]✗[/red] A git operation is still in progress in "
            f"[cyan]{repo_path}[/cyan]."
        )
        console.print(
            "  Finish (`git merge --continue`) or abort it first, then re-run."
        )
        return False

    remote = config.origin.remote_name
    base_ref = f"{remote}/{base_branch}"

    if not remote_branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] not "
            f"found on [cyan]{remote}[/cyan] (already fetched). "
            "Cannot merge."
        )
        return False

    ai_active = resolve_conflicts and config.ai_resolve.enabled
    if ai_active:
        console.print(
            f"[dim]AI conflict resolver: enabled "
            f"(command='{config.ai_resolve.command}', "
            f"prompt='{config.ai_resolve.merge_prompt_file}', "
            f"max_iterations={config.ai_resolve.max_iterations})[/dim]"
        )
    else:
        why = (
            "disabled via --no-resolve-conflicts" if not resolve_conflicts
            else "disabled in config"
        )
        console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    console.print(
        f"\n[bold]Phase:[/bold] Merging [cyan]{base_ref}[/cyan] "
        f"into tracked PR branches"
    )

    candidates: list[tuple[str, FeatureState]] = []
    for fid, fs in state.features.items():
        if fs.status == "skipped":
            continue
        if not fs.branch_name or not fs.rebase_pr_url:
            continue
        candidates.append((fid, fs))

    if not candidates:
        console.print(
            "  [dim]No tracked PRs with a branch + rebase PR URL — nothing "
            "to do.[/dim]"
        )
        return True

    any_unresolved = False
    for fid, fs in candidates:
        outcome = _process_one(
            config, repo_path, state, fid, fs, base_branch, base_ref,
            remote, ai_active,
        )
        if outcome == "conflict":
            any_unresolved = True
        # _process_one already persisted state/board on every meaningful
        # transition; nothing to do here.

    console.print(
        f"\n[bold]PR-conflict pass complete.[/bold] "
        f"{len(candidates)} tracked PR(s) inspected."
    )
    if any_unresolved:
        console.print(
            "[yellow]Some PRs are still in conflict — see above. Resolve "
            "them on GitHub (or locally + push), then re-run.[/yellow]"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Per-PR worker
# ---------------------------------------------------------------------------


def _process_one(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    fid: str,
    fs: FeatureState,
    base_branch: str,
    base_ref: str,
    remote: str,
    ai_active: bool,
) -> str:
    """Drive merge + (optional) AI resolve for ONE tracked PR.

    Returns one of:
      * ``"clean"``   — merge had nothing to do or was a clean merge we
                         deliberately discarded (per spec we only push
                         on conflict-resolution).
      * ``"resolved"`` — there was a conflict and the AI resolved it;
                         the merge commit was pushed.
      * ``"conflict"`` — conflict could not be resolved (AI off, AI gave
                         up, or some other failure); local branch reset
                         to its original tip and state flipped to
                         ``conflict``.
      * ``"skipped"`` — nothing actionable (branch missing on remote,
                         unrecoverable checkout failure, …).
    """
    branch = fs.branch_name
    assert branch is not None  # caller filtered

    label = (
        f"PR #{fs.pr_number}" if fs.pr_number else fid
    )
    rebase_label = (
        fs.rebase_pr_url.rsplit("/", 1)[-1]
        if fs.rebase_pr_url else "?"
    )
    console.print(
        f"\n  [cyan]{branch}[/cyan]  "
        f"[dim](source {label} → rebase PR #{rebase_label})[/dim]"
    )

    if not remote_branch_exists(repo_path, branch, remote):
        console.print(
            f"    [yellow]−[/yellow] branch missing on "
            f"[cyan]{remote}[/cyan], skipping"
        )
        return "skipped"

    start_sha = _refresh_local_branch(repo_path, branch, remote)
    if start_sha is None:
        console.print(
            f"    [yellow]−[/yellow] could not check out "
            f"[cyan]{branch}[/cyan], skipping"
        )
        return "skipped"

    # Attempt the merge. ``--no-ff`` mirrors GitHub's "Update branch"
    # button — we want a real merge commit so the PR explicitly records
    # the conflict-resolution decision in its history.
    merge_msg = f"Merge {base_ref} into {branch}"
    merge = run_git(
        ["merge", "--no-ff", "--no-edit", "-m", merge_msg, base_ref],
        repo_path, check=False,
    )

    if merge.returncode == 0:
        # Clean merge or already up-to-date.
        new_sha = run_git(
            ["rev-parse", "--verify", "HEAD"], repo_path, check=False,
        )
        new_head = new_sha.stdout.strip() if new_sha.returncode == 0 else start_sha
        if new_head != start_sha:
            console.print(
                "    [dim]clean merge — no conflicts, leaving the PR "
                "untouched (use the GitHub 'Update branch' button to "
                "publish a fresh merge if you want one)[/dim]"
            )
            run_git(["reset", "--hard", start_sha], repo_path, check=False)
        else:
            console.print(
                "    [dim]already up-to-date with "
                f"[cyan]{base_ref}[/cyan][/dim]"
            )
        # If the entry was previously marked as conflict but now merges
        # cleanly (someone else fixed it), surface that by clearing the
        # conflict markers on the local entry. Status itself is left
        # alone — the entry is still tracked, GitHub will reflect the
        # mergeable state on its own.
        if fs.status == "conflict" and fs.conflict_files:
            fs.conflict_files = []
            _persist(config, state)
        return "clean"

    conflict_files = get_conflict_files(repo_path)
    if not conflict_files:
        # Merge failed for some other reason — abort and warn so the
        # user can investigate manually instead of us silently flipping
        # the entry to "conflict" with no useful info.
        msg = (merge.stderr or "").strip().splitlines()[:3]
        _abort_any_merge(repo_path, start_sha)
        console.print(
            "    [yellow]![/yellow] merge failed without producing "
            "conflict markers — leaving PR alone."
        )
        for line in msg:
            console.print(f"      [dim]{line}[/dim]")
        return "skipped"

    console.print(
        f"    [red]✗[/red] {len(conflict_files)} conflicted file(s) "
        f"after merging [cyan]{base_ref}[/cyan]:"
    )
    for cf in conflict_files:
        console.print(f"      [red]•[/red] {cf}")

    if not ai_active:
        _abort_any_merge(repo_path, start_sha)
        _record_conflict(config, state, fs, conflict_files)
        return "conflict"

    source_pr = _synthesise_source_pr(fs)
    if source_pr is None:
        # Shouldn't normally happen — we only iterate entries with
        # ``pr_url`` set elsewhere — but be defensive: without source
        # PR context the prompt has nothing to ground the resolution
        # in, so don't even attempt it.
        _abort_any_merge(repo_path, start_sha)
        console.print(
            "    [yellow]![/yellow] no source PR metadata — cannot "
            "build resolver prompt, marking conflict."
        )
        _record_conflict(config, state, fs, conflict_files)
        return "conflict"

    ctx = AIResolveContext(
        port_branch=branch,
        base_branch=base_branch,
        source_pr=source_pr,
        conflict_files=conflict_files,
        start_sha=start_sha,
        operation="merge",
        rebase_pr_url=fs.rebase_pr_url,
    )

    result = attempt_ai_resolve(config, repo_path, ctx)

    # Cost is billed even when the resolve failed — accumulate before
    # branching on success so the project board reflects what we spent.
    if result.cost_usd is not None:
        prior = fs.ai_cost_usd or 0.0
        fs.ai_cost_usd = prior + result.cost_usd

    if not result.success:
        reason = result.error or (
            "timed out" if result.timed_out else "unknown failure"
        )
        cost_note = (
            f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
            if result.cost_usd is not None else ""
        )
        console.print(
            f"    [yellow]AI resolve failed:[/yellow] {reason}{cost_note}"
        )
        # ``attempt_ai_resolve`` already aborted + reset on failure, so
        # the worktree is back at start_sha. Just record the status.
        _record_conflict(config, state, fs, conflict_files)
        return "conflict"

    # Push the merge commit. Plain (non-force) push is correct: local
    # is start_sha + merge commit, origin is at start_sha — fast-forward.
    # Falling back to ``force_push`` here would risk clobbering any new
    # commit the PR author themselves pushed between our fetch and our
    # push.
    push = run_git(
        ["push", remote, branch], repo_path, check=False,
    )
    if push.returncode != 0:
        # Don't flip status to conflict — the resolution itself worked,
        # the push race is a separate problem the user can retry.
        console.print(
            f"    [yellow]![/yellow] merge resolved locally but push "
            f"failed (origin moved? auth?). Leaving local commit; "
            f"re-run to retry."
        )
        for line in (push.stderr or "").strip().splitlines()[:3]:
            console.print(f"      [dim]{line}[/dim]")
        # Persist the cost we already incurred even though the push will
        # be retried — losing it would understate the bill on the project
        # board if the user fixed the push problem out-of-band.
        if result.cost_usd is not None:
            _persist(config, state)
        return "skipped"

    iters = (
        f" (iterations: {result.iterations})" if result.iterations else ""
    )
    cost = (
        f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
        if result.cost_usd is not None else ""
    )
    console.print(
        f"    [green]✓[/green] AI resolved + pushed [cyan]{branch}[/cyan]"
        f"{iters}{cost}"
    )

    # Promote any prior "conflict" status back to needs_review since the
    # PR is now mergeable again. Also record AI involvement so the
    # status table / board show the magenta marker.
    fs.conflict_files = []
    if fs.status == "conflict":
        fs.status = "needs_review"
        # Clear the cherry-pick-failure markers if they were left over
        # from an earlier ``releasy run`` that punted into conflict
        # state — once the PR is mergeable they no longer apply.
        fs.failed_step_index = None
        fs.partial_pr_count = None
    fs.ai_resolved = True
    if result.iterations:
        prior = fs.ai_iterations or 0
        fs.ai_iterations = prior + result.iterations
    _persist(config, state)
    return "resolved"


def _record_conflict(
    config: Config,
    state: PipelineState,
    fs: FeatureState,
    conflict_files: list[str],
) -> None:
    """Flip a tracked PR to ``conflict`` and persist.

    Leaves ``branch_name`` / ``rebase_pr_url`` intact — the PR and its
    branch still exist on origin; only the *mergeability* changed.
    Doesn't touch ``failed_step_index`` / ``partial_pr_count`` either:
    those describe cherry-pick-time failures, not merge-time ones, and
    overwriting them would lose history.
    """
    fs.status = "conflict"
    fs.conflict_files = conflict_files
    _persist(config, state)

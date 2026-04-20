"""Port-cherry-pick pipeline.

Assumes the base branch (e.g. ``antalya-26.3``) already exists on origin.
For each source PR discovered via configured labels, the pipeline creates a
port branch off ``origin/<base_branch>``, cherry-picks the PR merge commit,
and on conflict either invokes the AI resolver or — if the resolver is
disabled or fails — drops the local branch (singletons / first-of-group)
or opens a draft PR labelled ``ai-needs-attention`` (partial groups), then
records the entry as ``Conflict`` in the GitHub Project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from releasy.config import Config, FeatureConfig, PRGroupConfig, PRSourceConfig
from releasy.git_ops import (
    OperationResult,
    abort_in_progress_op,
    append_commit_trailer,
    branch_exists,
    local_branch_exists,
    remote_branch_exists,
    cherry_pick_merge_commit,
    create_branch_from_ref,
    ensure_work_repo,
    fetch_commit,
    fetch_pr_ref,
    fetch_remote,
    force_push,
    is_operation_in_progress,
    ref_exists_locally,
    run_git,
    stash_and_clean,
    update_submodules,
)
from releasy.github_ops import (
    PRInfo,
    add_label_to_pr,
    create_pull_request,
    ensure_label,
    fetch_pr_by_number,
    find_pr_for_branch,
    get_origin_repo_slug,
    is_pr_merged,
    parse_pr_url,
    pr_ref_label,
    require_origin_repo_slug,
    search_prs_by_labels,
    slug_to_https_url,
    sync_project,
    update_pull_request,
)
from releasy.state import FeatureState, PipelineState, load_state, save_state

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist_state(config: Config, state: PipelineState) -> None:
    """Persist state to the per-project state file and (optionally) sync the project board."""
    save_state(state, config)
    if config.push:
        sync_project(config, state)


def _setup_repo(
    config: Config, work_dir: Path | None, base_branch: str | None = None,
) -> Path:
    """Set up work repo and fetch origin. The ``onto`` argument is used
    only for branch naming.

    If the repo was just cloned and ``base_branch`` is provided, check it
    out from origin and initialise submodules — saves the user (and Claude)
    from doing it manually before the first build.
    """
    wd = config.resolve_work_dir(work_dir)
    console.print(f"[dim]Working directory: {wd}[/dim]")

    console.print("[dim]Setting up repository...[/dim]")
    repo_path, freshly_cloned = ensure_work_repo(config, wd)
    console.print(f"[dim]Repo: {repo_path}[/dim]")

    console.print(f"Fetching [cyan]{config.origin.remote_name}[/cyan]...", end=" ")
    fetch_remote(repo_path, config.origin.remote_name)
    console.print("[green]done[/green]")

    if freshly_cloned and base_branch:
        remote = config.origin.remote_name
        base_ref = f"{remote}/{base_branch}"
        if branch_exists(repo_path, base_branch, remote):
            console.print(
                f"Checking out base branch [cyan]{base_branch}[/cyan]...", end=" ",
            )
            run_git(
                ["checkout", "-B", base_branch, base_ref], repo_path, check=False,
            )
            console.print("[green]done[/green]")
        console.print(
            "[dim]Initialising submodules (this can take a few minutes)...[/dim]",
        )
        update_submodules(repo_path)
        console.print("[green]Submodules ready[/green]")

    return repo_path


def _push(config: Config, repo_path: Path, branch: str) -> None:
    """Push a branch to the origin remote (the only push path we use)."""
    force_push(repo_path, branch, config)


# ---------------------------------------------------------------------------
# Feature units (singletons + sequential PR groups)
# ---------------------------------------------------------------------------


@dataclass
class FeatureUnit:
    """A logical unit of porting work — one PR or a sequential group.

    For singletons, ``prs`` contains exactly one ``PRInfo``. For groups,
    ``prs`` are listed in cherry-pick order.
    """
    feature_id: str
    prs: list[PRInfo]
    if_exists: str
    title_prefix: str = ""        # used for both single-PR and group titles
    is_group: bool = False
    group_id: str | None = None   # filled when is_group
    # Mutable per-run bookkeeping (set by _process_feature_unit):
    ai_resolved_count: int = 0
    ai_iterations_total: int = 0
    # Sum of USD cost reported by Claude across every cherry-pick step
    # in this unit (groups accumulate; singletons hit at most one step).
    # ``None`` until Claude reports a cost at least once — keeps
    # downstream code able to distinguish "AI ran but produced no cost
    # data" (None) from "AI ran and the bill was 0.0".
    ai_cost_usd_total: float | None = None

    @property
    def sort_key(self) -> tuple[str, int]:
        """Earliest merged_at across constituent PRs, fallback to PR number."""
        merged = [pr.merged_at for pr in self.prs if pr.merged_at]
        first = min(merged) if merged else "9999"
        return (first, min(pr.number for pr in self.prs))

    def primary_pr(self) -> PRInfo:
        return self.prs[0]


PRRef = tuple[str, str, int]


def _singleton_feature_id(pr: PRInfo, origin_slug: str | None) -> str:
    """Branch / state ID for a single-PR port.

    Origin PRs keep the historical short ``pr-<N>`` form so existing state
    files and branches keep working. PRs from other repos get a slug-prefixed
    ID (``<owner>-<repo>-pr-<N>``) so a same-numbered origin PR can't clash
    with them.
    """
    if origin_slug and pr.repo_slug == origin_slug:
        return f"pr-{pr.number}"
    owner, repo = pr.repo_slug.split("/", 1)
    return f"{owner}-{repo}-pr-{pr.number}"


def _author_filter_reason(
    author: str | None,
    included_authors: set[str],
    excluded_authors: set[str],
) -> str | None:
    """Return a human-readable reason to drop a PR by author, or ``None``
    if the PR survives the author filter.

    ``included_authors`` and ``excluded_authors`` are lower-cased GitHub
    logins. When ``included_authors`` is non-empty it's an allowlist:
    PRs whose author is unknown or not on the list are dropped.
    """
    login = (author or "").lower()
    if excluded_authors and login and login in excluded_authors:
        return f"author @{author} is in pr_sources.exclude_authors"
    if included_authors:
        if not login:
            return (
                "author is unknown but pr_sources.include_authors is set"
            )
        if login not in included_authors:
            return (
                f"author @{author} is not in pr_sources.include_authors"
            )
    return None


def _build_singleton_units(
    config: Config,
    collected: dict[PRRef, tuple[PRInfo, PRSourceConfig]],
) -> list[FeatureUnit]:
    units: list[FeatureUnit] = []
    origin_slug = get_origin_repo_slug(config)
    for pr, src in collected.values():
        units.append(FeatureUnit(
            feature_id=_singleton_feature_id(pr, origin_slug),
            prs=[pr],
            if_exists=src.if_exists,
            title_prefix=src.description,
        ))
    return units


def _build_group_units(
    config: Config,
    excluded_pr_refs: set[PRRef],
    excluded_labels: set[str],
    included_authors: set[str],
    excluded_authors: set[str],
) -> tuple[list[FeatureUnit], set[PRRef]]:
    """Materialise group units, fetching each PR. Returns (units, claimed_refs)
    where ``claimed_refs`` are ``(owner, repo, number)`` tuples that should
    NOT also appear as standalone units.

    ``included_authors`` and ``excluded_authors`` are lower-cased GitHub
    logins; they filter group members the same way they filter top-level
    discovered PRs.
    """
    origin_slug = get_origin_repo_slug(config)
    units: list[FeatureUnit] = []
    claimed: set[PRRef] = set()
    for group in config.pr_sources.groups:
        console.print(
            f"\n  Resolving group [yellow]{group.id}[/yellow] "
            f"({len(group.prs)} PR(s))"
        )
        group_prs: list[PRInfo] = []
        for url in group.prs:
            parsed = parse_pr_url(url)
            if parsed is None:
                console.print(f"    [red]✗[/red] Bad PR URL: {url}")
                continue
            owner, repo, num = parsed
            ref_label = pr_ref_label(f"{owner}/{repo}", num, origin_slug)
            if parsed in excluded_pr_refs:
                console.print(
                    f"    [yellow]−[/yellow] {ref_label} excluded via "
                    "pr_sources.exclude_prs, dropping from group"
                )
                continue
            pr = fetch_pr_by_number(config, num, slug=f"{owner}/{repo}")
            if pr is None:
                console.print(f"    [red]✗[/red] Could not fetch PR {ref_label}")
                continue
            if excluded_labels and (set(pr.labels) & excluded_labels):
                console.print(
                    f"    [yellow]−[/yellow] {ref_label} carries an excluded "
                    "label, dropping from group"
                )
                continue
            reason = _author_filter_reason(
                pr.author, included_authors, excluded_authors,
            )
            if reason is not None:
                console.print(
                    f"    [yellow]−[/yellow] {ref_label} {reason}, "
                    "dropping from group"
                )
                continue
            group_prs.append(pr)
            claimed.add(parsed)
            console.print(
                f"    [dim]+ {ref_label}[/dim] {pr.title} [{pr.state}]"
            )
        if not group_prs:
            console.print(
                f"    [yellow]Group {group.id!r} has no PRs left after "
                "filtering, skipping[/yellow]"
            )
            continue
        units.append(FeatureUnit(
            feature_id=group.id,
            prs=group_prs,
            if_exists=group.if_exists,
            title_prefix=group.description,
            is_group=True,
            group_id=group.id,
        ))
    return units, claimed


def _prune_superseded_singletons(config: Config, state: PipelineState) -> bool:
    """Drop singleton state entries for PRs now claimed by a ``pr_sources.group``.

    When a PR that used to be ported as its own feature (``pr-<N>``) is
    moved into a group entry in ``pr_sources.groups``, the group becomes the
    unit of work (``feature/<base>/<group-id>``) and the old singleton state
    entry is stale. Left alone, ``continue`` would keep pushing it and opening
    a duplicate PR alongside the group's combined PR.

    This helper removes those stale singleton entries from state. Any branches
    / PRs they already produced are left on GitHub — the user decides whether
    to close them.

    Returns True if any entry was removed (so the caller can persist state).
    """
    group_pr_refs: set[PRRef] = set()
    group_feature_ids: set[str] = set()
    for group in config.pr_sources.groups:
        group_feature_ids.add(group.id)
        for url in group.prs:
            parsed = parse_pr_url(url)
            if parsed is not None:
                group_pr_refs.add(parsed)

    if not group_pr_refs:
        return False

    origin_slug = get_origin_repo_slug(config)
    stale: list[tuple[str, FeatureState, PRRef]] = []
    for fid, fs in state.features.items():
        if fid in group_feature_ids:
            continue  # the group's own state entry
        if len(fs.pr_numbers) > 1:
            continue  # a different multi-PR unit
        if not fs.pr_url:
            continue
        parsed = parse_pr_url(fs.pr_url)
        if parsed is not None and parsed in group_pr_refs:
            stale.append((fid, fs, parsed))

    for fid, fs, ref in stale:
        owner, repo, num = ref
        ref_label = pr_ref_label(f"{owner}/{repo}", num, origin_slug)
        extra = ""
        if fs.rebase_pr_url:
            extra = (
                f" — open rebase PR {fs.rebase_pr_url} left untouched; "
                "close it manually if superseded by the group PR"
            )
        console.print(
            f"  [yellow]⚠[/yellow] Dropping stale singleton "
            f"[cyan]{fid}[/cyan] (PR {ref_label} is now in a group){extra}"
        )
        del state.features[fid]

    return bool(stale)


# ---------------------------------------------------------------------------
# PR discovery (shared by run_pipeline and import_state)
# ---------------------------------------------------------------------------


def discover_feature_units(config: Config) -> list["FeatureUnit"]:
    """Discover the PR units defined by ``config.pr_sources``.

    Pure discovery: hits GitHub but touches no git worktree. Returns the
    fully-filtered, ordered list of :class:`FeatureUnit`s the pipeline
    would attempt to port, so other commands (``releasy import``) can
    rebuild state from the same source-of-truth definition.

    Output (``console.print``) mirrors ``releasy run``'s "Phase: Porting"
    discovery block — this helper is called in place of the inline block
    that used to live in :func:`run_pipeline`.
    """
    origin_slug = get_origin_repo_slug(config)

    # --- Collect PRs from all sources (union) ---
    # Keyed by (owner, repo, number) so cross-repo PR references can never
    # collide with same-numbered origin PRs.
    collected: dict[PRRef, tuple[PRInfo, PRSourceConfig]] = {}
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
            ref = pr.ref()
            if ref not in collected:
                collected[ref] = (pr, pr_source)

    prs_cfg = config.pr_sources
    include_pr_refs: set[PRRef] = {
        parsed
        for url in prs_cfg.include_prs
        if (parsed := parse_pr_url(url)) is not None
    }
    exclude_pr_refs: set[PRRef] = {
        parsed
        for url in prs_cfg.exclude_prs
        if (parsed := parse_pr_url(url)) is not None
    }

    if prs_cfg.exclude_labels:
        exclude_set = set(prs_cfg.exclude_labels)
        before = len(collected)
        collected = {
            ref: (pr, src)
            for ref, (pr, src) in collected.items()
            if not (set(pr.labels) & exclude_set) or ref in include_pr_refs
        }
        removed = before - len(collected)
        if removed:
            console.print(
                f"\n  [dim]Excluded {removed} PR(s) by label filter "
                f"({', '.join(prs_cfg.exclude_labels)})[/dim]"
            )

    included_authors = {a.lower() for a in prs_cfg.include_authors if a}
    excluded_authors = {a.lower() for a in prs_cfg.exclude_authors if a}
    if included_authors or excluded_authors:
        kept: dict[PRRef, tuple[PRInfo, PRSourceConfig]] = {}
        dropped: list[tuple[PRInfo, str]] = []
        for ref, (pr, src) in collected.items():
            if ref in include_pr_refs:
                kept[ref] = (pr, src)
                continue
            reason = _author_filter_reason(
                pr.author, included_authors, excluded_authors,
            )
            if reason is None:
                kept[ref] = (pr, src)
            else:
                dropped.append((pr, reason))
        collected = kept
        if dropped:
            console.print(
                f"\n  [dim]Excluded {len(dropped)} PR(s) by author filter:"
                "[/dim]"
            )
            for pr, reason in dropped:
                ref_label = pr_ref_label(
                    pr.repo_slug, pr.number, origin_slug,
                )
                console.print(f"    [dim]− {ref_label}: {reason}[/dim]")

    if include_pr_refs:
        default_source = (
            config.pr_sources.by_labels[0]
            if config.pr_sources.by_labels
            else PRSourceConfig(labels=[], if_exists=config.pr_sources.if_exists)
        )
        for ref in sorted(include_pr_refs):
            if ref in collected:
                continue
            owner, repo, pr_num = ref
            ref_label = pr_ref_label(f"{owner}/{repo}", pr_num, origin_slug)
            console.print(f"\n  Fetching explicitly included PR {ref_label}...")
            pr_info = fetch_pr_by_number(config, pr_num, slug=f"{owner}/{repo}")
            if pr_info:
                collected[ref] = (pr_info, default_source)
                console.print(f"    [green]✓[/green] {pr_info.title}")
            else:
                console.print(f"    [red]✗[/red] Could not fetch PR {ref_label}")

    for ref in exclude_pr_refs:
        if ref in collected:
            pr_info, _ = collected.pop(ref)
            owner, repo, pr_num = ref
            ref_label = pr_ref_label(f"{owner}/{repo}", pr_num, origin_slug)
            console.print(
                f"\n  [dim]Excluded PR {ref_label} ({pr_info.title})[/dim]"
            )

    # --- Build sequential PR groups (each becomes one combined feature) ---
    excluded_label_set = set(prs_cfg.exclude_labels)
    group_units, claimed_pr_refs = _build_group_units(
        config, exclude_pr_refs, excluded_label_set,
        included_authors, excluded_authors,
    )
    # Drop any singletons that the groups have claimed.
    for ref in claimed_pr_refs:
        if ref in collected:
            pr_info, _ = collected.pop(ref)
            owner, repo, pr_num = ref
            ref_label = pr_ref_label(f"{owner}/{repo}", pr_num, origin_slug)
            console.print(
                f"  [dim]{ref_label} ({pr_info.title}) belongs to a group — "
                "removed from singletons[/dim]"
            )

    units: list[FeatureUnit] = (
        _build_singleton_units(config, collected) + group_units
    )
    units.sort(key=lambda u: u.sort_key)
    return units


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
    state = load_state(config)
    _prune_superseded_singletons(config, state)
    repo_path = _setup_repo(config, work_dir, config.base_branch_name(onto))

    if is_operation_in_progress(repo_path):
        if config.pr_sources.if_exists == "recreate":
            kind = abort_in_progress_op(repo_path)
            console.print(
                f"\n[yellow]↻ Aborted in-progress {kind} in [cyan]{repo_path}[/cyan][/yellow] "
                f"(pr_sources.if_exists: recreate)"
            )
        else:
            console.print(
                f"\n[red]✗[/red] A cherry-pick/merge/rebase is already in progress "
                f"in [cyan]{repo_path}[/cyan]."
            )
            console.print(
                "  Resolve it first (or run `git cherry-pick --abort`), then re-run.\n"
                "  Or set [cyan]pr_sources.if_exists: recreate[/cyan] in config to "
                "auto-abort it."
            )
            raise SystemExit(2)

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
    console.print(
        f"PRs will be opened against [bold cyan]{require_origin_repo_slug(config)}[/bold cyan] "
        "(origin) — RelEasy never opens PRs against any other repo."
    )

    state.set_started(onto)
    state.base_branch = base_branch
    state.phase = "init"
    _persist_state(config, state)

    if config.push:
        ensure_label(
            config, RELEASY_LABEL, RELEASY_LABEL_COLOR, RELEASY_LABEL_DESCRIPTION,
        )

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

    # The needs-attention label is used for partial-group draft PRs whenever
    # an unresolved conflict surfaces, regardless of whether the AI resolver
    # was enabled — pre-create it so the PR-creation path doesn't fail on a
    # missing label.
    if config.push:
        ensure_label(
            config,
            config.ai_resolve.needs_attention_label,
            config.ai_resolve.needs_attention_label_color,
            "Releasy stopped on a conflict it could not resolve — needs human review",
        )

    console.print(
        f"\n[bold]Phase:[/bold] Porting PRs onto [cyan]{base_branch}[/cyan]"
    )

    units = discover_feature_units(config)

    if not units:
        console.print("\n  [dim]No PRs or groups to process after filtering[/dim]")

    existing_ids = {f.id for f in config.features}
    for unit in units:
        if unit.feature_id in existing_ids:
            continue
        existing_ids.add(unit.feature_id)
        # _process_feature_unit always returns "continue" — unresolved
        # conflicts are now handled in-place (drop the branch or open a
        # draft PR), and the pipeline keeps moving so a single bad PR
        # can't strand the whole queue.
        _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active,
        )

    state.phase = "ports_done"
    _persist_state(config, state)

    # --- Summary ---
    console.print(f"\n[bold]Pipeline complete.[/bold] Phase: {state.phase}")
    if state.base_branch:
        console.print(f"  Base branch: [cyan]{state.base_branch}[/cyan]")
    ready = sum(
        1 for fs in state.features.values() if fs.status == "needs_review"
    )
    branch_only = sum(
        1 for fs in state.features.values() if fs.status == "branch_created"
    )
    ai_assisted = sum(
        1 for fs in state.features.values()
        if fs.status in ("needs_review", "branch_created") and fs.ai_resolved
    )
    conflicts = sum(
        1 for fs in state.features.values() if fs.status == "conflict"
    )
    if ready or branch_only or conflicts:
        console.print(
            f"  Ports: {ready} needs-review, {branch_only} branch-created, "
            f"{conflicts} conflict ({ai_assisted} ai-assisted)"
        )

    return state


# ---------------------------------------------------------------------------
# Sequential mode
# ---------------------------------------------------------------------------


def run_sequential(
    config: Config,
    onto: str,
    work_dir: Path | None = None,
    resolve_conflicts: bool = True,
) -> PipelineState:
    """Process the merged-time-sorted PR queue one PR per invocation.

    Loop semantics — for each unit in ``discover_feature_units(config)``
    order (already sorted by ``merged_at``):

      * state ``merged`` / ``skipped``     → already done, skip.
      * state ``needs_review`` /
        ``branch_created`` with rebase PR  → ask GitHub if the PR is
                                            merged. Yes → mark
                                            ``merged`` in state and
                                            continue. No → exit 1 with
                                            the in-flight PR URL.
                                            Lookup failure → exit 1.
      * state ``branch_created`` with no
        rebase PR                           → exit 1 (something went
                                            wrong opening the PR — fix
                                            it and retry).
      * state ``conflict``                  → exit 1 with the conflict
                                            files (sequential mode never
                                            auto-retries).
      * no state at all                    → port it (cherry-pick onto
                                            the freshly-fetched
                                            ``origin/<base>``, push, open
                                            PR), then return so the
                                            caller exits cleanly.

    ``releasy run`` and ``releasy continue`` (no ``--branch``) both
    dispatch here when ``config.sequential`` is true; the function
    is the single source of truth for sequential-mode behaviour.
    """
    state = load_state(config)
    _prune_superseded_singletons(config, state)
    repo_path = _setup_repo(config, work_dir, config.base_branch_name(onto))

    if is_operation_in_progress(repo_path):
        if config.pr_sources.if_exists == "recreate":
            kind = abort_in_progress_op(repo_path)
            console.print(
                f"\n[yellow]↻ Aborted in-progress {kind} in [cyan]{repo_path}[/cyan][/yellow] "
                f"(pr_sources.if_exists: recreate)"
            )
        else:
            console.print(
                f"\n[red]✗[/red] A cherry-pick/merge/rebase is already in progress "
                f"in [cyan]{repo_path}[/cyan]."
            )
            console.print(
                "  Resolve it first (or run `git cherry-pick --abort`), then re-run.\n"
                "  Or set [cyan]pr_sources.if_exists: recreate[/cyan] in config to "
                "auto-abort it."
            )
            raise SystemExit(2)

    base_branch = config.base_branch_name(onto)
    remote = config.origin.remote_name

    if not branch_exists(repo_path, base_branch, remote):
        console.print(
            f"\n[red]✗[/red] Base branch [cyan]{base_branch}[/cyan] does not exist "
            f"on remote [cyan]{remote}[/cyan].\n"
            f"  Create and push it first, then re-run."
        )
        raise SystemExit(2)

    base_ref = f"{remote}/{base_branch}"
    console.print(f"Base: [cyan]{base_ref}[/cyan]")
    console.print(
        "[bold]Mode:[/bold] [cyan]sequential[/cyan] "
        "(one PR per invocation; previous PR must merge before the next)"
    )
    console.print(
        f"PRs will be opened against [bold cyan]{require_origin_repo_slug(config)}[/bold cyan] "
        "(origin) — RelEasy never opens PRs against any other repo."
    )

    state.set_started(onto)
    state.base_branch = base_branch
    state.phase = "init"
    _persist_state(config, state)

    if config.push:
        ensure_label(
            config, RELEASY_LABEL, RELEASY_LABEL_COLOR, RELEASY_LABEL_DESCRIPTION,
        )

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
        why = (
            "disabled via --no-resolve-conflicts" if not resolve_conflicts
            else "disabled in config"
        )
        console.print(f"[dim]AI conflict resolver: {why}[/dim]")

    if config.push:
        ensure_label(
            config,
            config.ai_resolve.needs_attention_label,
            config.ai_resolve.needs_attention_label_color,
            "Releasy stopped on a conflict it could not resolve — needs human review",
        )

    console.print(
        f"\n[bold]Phase:[/bold] Sequential porting onto [cyan]{base_branch}[/cyan]"
    )

    units = discover_feature_units(config)
    if not units:
        console.print("\n  [dim]No PRs to process after filtering[/dim]")
        state.phase = "ports_done"
        _persist_state(config, state)
        return state

    origin_slug = get_origin_repo_slug(config)

    for unit in units:
        fs = state.features.get(unit.feature_id)
        primary = unit.primary_pr()
        ref = pr_ref_label(primary.repo_slug, primary.number, origin_slug)

        if fs is not None and fs.status in ("merged", "skipped"):
            console.print(
                f"  [dim]{unit.feature_id} ({ref}) — {fs.status}, skipping[/dim]"
            )
            continue

        if fs is not None and fs.status == "conflict":
            console.print(
                f"\n[red]✗[/red] [cyan]{unit.feature_id}[/cyan] ({ref}) is in "
                "[red]conflict[/red] state — sequential mode will not advance "
                "until it is resolved."
            )
            if fs.conflict_files:
                for cf in fs.conflict_files:
                    console.print(f"      [red]•[/red] {cf}")
            console.print(
                "  Resolve it manually, then re-run [cyan]releasy continue[/cyan]."
            )
            raise SystemExit(1)

        if fs is not None and fs.status in ("needs_review", "branch_created"):
            if not fs.rebase_pr_url:
                console.print(
                    f"\n[red]✗[/red] [cyan]{unit.feature_id}[/cyan] ({ref}) has "
                    f"status [yellow]{fs.status}[/yellow] but no rebase PR URL "
                    "on file — cannot determine merge state."
                )
                console.print(
                    "  Open the PR manually (or run "
                    "[cyan]releasy continue --branch <id>[/cyan]) and try again."
                )
                raise SystemExit(1)

            console.print(
                f"\n  Checking in-flight PR for [cyan]{unit.feature_id}[/cyan] "
                f"({ref}): [link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link]"
            )
            merged = is_pr_merged(config, fs.rebase_pr_url)
            if merged is None:
                console.print(
                    f"\n[red]✗[/red] Could not determine merge state of "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link]. "
                    "Check RELEASY_GITHUB_TOKEN / network and retry."
                )
                raise SystemExit(1)
            if not merged:
                console.print(
                    f"\n[red]✗[/red] Rebase PR "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link] is "
                    "[yellow]not merged yet[/yellow]."
                )
                console.print(
                    "  Sequential mode requires it to merge into "
                    f"[cyan]{base_branch}[/cyan] before the next port. "
                    "Approve and merge it, then re-run."
                )
                raise SystemExit(1)

            fs.status = "merged"
            fs.conflict_files = []
            console.print(
                f"    [green]✓[/green] PR merged — advancing the queue"
            )
            _persist_state(config, state)
            continue

        # No state yet — this is the next unit to port. Refresh remote so
        # the new branch is created off the latest base (which now
        # includes any previously-merged sequential ports).
        console.print(
            f"\n[bold]Porting next unit:[/bold] [cyan]{unit.feature_id}[/cyan] ({ref})"
        )
        console.print(
            f"  [dim]Re-fetching {remote} so the port branch is based on "
            f"the current {base_ref}...[/dim]"
        )
        fetch_remote(repo_path, remote)

        existing_ids = {f.id for f in config.features}
        if unit.feature_id in existing_ids:
            console.print(
                f"  [yellow]![/yellow] feature {unit.feature_id!r} already "
                "in config.features — skipping config-list mutation"
            )
            # _process_feature_unit appends to config.features; guard against
            # a duplicate by short-circuiting if it would clash. This matches
            # the run_pipeline behaviour (which also skips in this case).
            continue

        _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active,
        )

        # _process_feature_unit may have ended in either a clean port or
        # an unresolved conflict. Either way, sequential mode stops here:
        # the user reviews the PR (or fixes the conflict) before invoking
        # again.
        new_fs = state.features.get(unit.feature_id)
        if new_fs is not None and new_fs.status == "conflict":
            console.print(
                "\n[yellow]Sequential run stopped on an unresolved conflict.[/yellow] "
                "Fix it manually, then re-run [cyan]releasy continue[/cyan]."
            )
        else:
            console.print(
                "\n[bold]Sequential run paused.[/bold] Review and merge the new "
                "PR, then re-run [cyan]releasy continue[/cyan] to port the next one."
            )
        return state

    # Queue exhausted — nothing left.
    state.phase = "ports_done"
    _persist_state(config, state)
    console.print(
        "\n[green]✓ All sequential ports processed[/green] — queue is empty."
    )
    return state


def _unit_pr_meta(unit: FeatureUnit) -> dict:
    """Build state-meta dict for a unit (single PR or group).

    ``pr_author`` is the GitHub login of the *primary* (first) PR — for
    groups, that's the author of the first PR in cherry-pick order, per
    the user's spec for the project board's default ``Assignee Dev``.
    May be ``None`` when GitHub didn't expose the author (rare:
    deleted accounts).
    """
    primary = unit.primary_pr()
    return {
        "pr_url": primary.url,
        "pr_number": primary.number,
        "pr_title": primary.title,
        "pr_body": primary.body,
        "pr_numbers": [pr.number for pr in unit.prs],
        "pr_urls": [pr.url for pr in unit.prs],
        "pr_author": primary.author,
    }


_VERSION_TOKEN = r"v?\d+(?:\.\d+)+"
_RELEASY_PREFIX_RE = re.compile(r"^\[releasy\b[^\]]*\]\s*", re.IGNORECASE)

# Label automatically applied to every PR RelEasy opens or updates, so a
# project's GitHub UI can filter "everything releasy created/touched" at a
# glance — and so the title itself stays clean.
RELEASY_LABEL = "releasy"
RELEASY_LABEL_COLOR = "1F6FEB"  # GitHub blue
RELEASY_LABEL_DESCRIPTION = "Created/managed by RelEasy"


def _display_project(project: str | None) -> str:
    """Render ``config.project`` for inclusion in PR titles.

    Lower-case names get title-cased (``antalya`` → ``Antalya``,
    ``stable-26`` → ``Stable-26``); names that already carry mixed case
    are preserved verbatim (so e.g. ``ClickHouse`` stays ``ClickHouse``).
    """
    if not project:
        return ""
    if project.islower():
        return project.title()
    return project


def _version_label(project: str | None, base_branch: str | None) -> str:
    """Pull the version suffix out of the base branch name when possible.

    If ``base_branch`` follows the conventional ``<project>-<version>``
    shape (``antalya-26.3``), the bit after ``<project>-`` is the version
    label. Otherwise the whole branch name is used so the prefix still
    points at the real target — never silently drops information.
    """
    if not base_branch:
        return ""
    if project:
        prefix = f"{project.lower()}-"
        if base_branch.lower().startswith(prefix):
            return base_branch[len(prefix):]
    return base_branch


def _subject_prefix(project: str | None, base_branch: str | None) -> str:
    """Build the ``"Antalya 26.3"``-style PR title prefix.

    Falls back gracefully:

      - both pieces present → ``"Antalya 26.3"``
      - project only        → ``"Antalya"``
      - base only           → ``"antalya-26.3"``
      - neither             → ``""`` (caller handles)
    """
    proj = _display_project(project)
    ver = _version_label(project, base_branch)
    if proj and ver:
        return f"{proj} {ver}"
    return proj or ver or ""


def _strip_misleading_title_prefix(title: str, project: str | None) -> str:
    """Strip a misleading version-prefix from a source PR title.

    Source repos often title backport PRs with their own target version,
    e.g. ``"26.1 Antalya: Token Authentication and Authorization"`` for a
    PR landing on ``antalya-26.1``. When that PR is re-ported onto a
    different base (say ``antalya-26.3``), the embedded ``"26.1"`` becomes
    actively misleading in the rebase PR title.

    This helper drops, in order of preference:

      1. A leading ``[releasy …]`` tag from a previous run (so we never
         double-tag when porting one of our own rebase PRs).
      2. ``"<version> <project>[:|-] "`` (e.g. ``"26.1 Antalya: "``).
      3. ``"<project> <version>[:|-] "`` (e.g. ``"Antalya 26.1: "``).
      4. Bare ``"<version>[:|-] "`` (e.g. ``"v3.2 - "``).

    A bare leading version with no separator (``"26.1 Foo"``) is left
    alone — without a colon/dash we can't tell a target-version prefix
    from a genuine title.
    """
    cleaned = _RELEASY_PREFIX_RE.sub("", title.strip(), count=1)

    patterns: list[str] = []
    if project:
        proj = re.escape(project)
        patterns.append(rf"^{_VERSION_TOKEN}\s+{proj}\s*[:\-]\s+")
        patterns.append(rf"^{proj}\s+{_VERSION_TOKEN}\s*[:\-]\s+")
    patterns.append(rf"^{_VERSION_TOKEN}\s*[:\-]\s+")

    for pat in patterns:
        new = re.sub(pat, "", cleaned, count=1, flags=re.IGNORECASE)
        if new != cleaned:
            cleaned = new
            break

    return cleaned.strip() or title


def _unit_title(
    unit: FeatureUnit,
    project: str | None,
    base_branch: str | None,
) -> str:
    """Synthesise the PR title for a unit.

    Format: ``"<Project> <version>: <subject>"`` — e.g.
    ``"Antalya 26.3: Token Authentication and Authorization"``. The
    ``[releasy]`` text tag is gone; identification is done via the
    ``releasy`` label on the PR (see :data:`RELEASY_LABEL`).

    Source PR titles are sanitised via ``_strip_misleading_title_prefix``
    so a leading ``"26.1 Antalya: …"`` (the source's own target version)
    doesn't leak into a rebase PR that actually targets a different
    version.
    """
    prefix = _subject_prefix(project, base_branch)

    if unit.is_group:
        if unit.title_prefix:
            subject = unit.title_prefix.rstrip()
        elif len(unit.prs) == 1:
            subject = _strip_misleading_title_prefix(unit.prs[0].title, project)
        else:
            subject = (
                f"{unit.group_id}: combined port of {len(unit.prs)} PRs"
            )
    else:
        pr = unit.primary_pr()
        subject = _strip_misleading_title_prefix(pr.title, project)
        if unit.title_prefix:
            subject = f"{unit.title_prefix}{subject}"

    return f"{prefix}: {subject}" if prefix else subject


# Matches any level markdown heading: "# title", "## title", … up to 6.
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _extract_md_section(body: str, keyword: str) -> str | None:
    """Return the text under the first markdown heading whose title
    contains ``keyword`` (case-insensitive), up to the next heading or
    end of document. ``None`` if not found or section is empty.
    """
    if not body:
        return None
    key = keyword.lower()
    headings = list(_MD_HEADING_RE.finditer(body))
    for i, m in enumerate(headings):
        if key in m.group(2).lower():
            start = m.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
            section = body[start:end].strip()
            return section or None
    return None


def _extract_changelog_category(body: str) -> str | None:
    """Pull the single chosen 'Changelog category' value from a PR body.

    ClickHouse's template lists every category as a bullet and authors
    usually delete all but one. We return the first non-empty bullet (or
    plain line) we find under the heading; ``None`` if absent.
    """
    section = _extract_md_section(body, "changelog category")
    if not section:
        return None
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^[-*+]\s+(.+)$", stripped)
        text = (m.group(1) if m else stripped).strip()
        if text.startswith("(") or text.lower().startswith("leave one"):
            continue
        return text
    return None


def _extract_changelog_entry(body: str) -> str | None:
    """Pull the 'Changelog entry' paragraph from a PR body, stripped.

    Drops placeholder-only sections (e.g. the raw template hint line
    left behind when the author wrote nothing).
    """
    section = _extract_md_section(body, "changelog entry")
    if not section:
        return None
    # Some PR bodies contain an HTML comment or the default hint inside
    # the section when no entry was added. Remove obvious placeholders.
    cleaned = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL).strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    if low.startswith("...") or low in {"n/a", "na", "none", "-"}:
        return None
    return cleaned


def _build_changelog_block(unit: FeatureUnit) -> str | None:
    """Synthesise a 'Changelog category' + 'Changelog entry' block for
    the combined PR body.

    Rules (from user spec):
    - Category = first PR's category (fallback: any PR in the unit
      that does specify one, in listed order).
    - Entry = the first PR's changelog entry, with a ``(<url1> by
      <author1>, <url2> by <author2>, …)`` suffix listing every PR in
      the unit (regardless of whether a given PR contributed its own
      changelog entry). The suffix is added even for singletons — it
      gives reviewers one-click access to the source PR and its author.

    Returns ``None`` when no PR in the unit has either a category or
    an entry, so we don't clutter the body with empty headings.
    """
    category: str | None = None
    for pr in unit.prs:
        cat = _extract_changelog_category(pr.body or "")
        if cat:
            category = cat
            break

    entry_text: str | None = None
    for pr in unit.prs:
        entry = _extract_changelog_entry(pr.body or "")
        if entry:
            entry_text = entry.strip()
            break

    if not category and not entry_text:
        return None

    out: list[str] = []
    if category:
        out.append("### Changelog category (leave one):")
        out.append("")
        out.append(f"- {category}")
        out.append("")
    if entry_text:
        out.append(
            "### Changelog entry (a user-readable short description of the "
            "changes that goes to CHANGELOG.md):"
        )
        out.append("")
        attribution = _format_pr_attribution(unit.prs)
        final_entry = entry_text
        if attribution:
            # If the entry already ends with ')' keep them on the same
            # line; otherwise append with a space. Either way we strip a
            # trailing period before the paren so punctuation reads
            # cleanly.
            if final_entry.endswith("."):
                final_entry = final_entry[:-1]
            final_entry = f"{final_entry} ({attribution})."
        out.append(final_entry)
        out.append("")
    return "\n".join(out).rstrip()


def _format_pr_attribution(prs: "list[PRInfo]") -> str:
    """Build the ``<url> by <author>`` comma-separated attribution suffix.

    Falls back to just the URL when the author is unknown.
    """
    parts: list[str] = []
    for pr in prs:
        if pr.author:
            parts.append(f"{pr.url} by @{pr.author}")
        else:
            parts.append(pr.url)
    return ", ".join(parts)


def _unit_body(
    unit: FeatureUnit,
    origin_slug: str | None,
    *,
    needs_intervention: bool = False,
    failed_index: int | None = None,
    failed_pr: PRInfo | None = None,
    conflict_files: list[str] | None = None,
) -> str:
    """Build the PR body listing constituent PRs.

    Origin-repo PRs are referenced as ``#N`` so GitHub auto-links them in
    the destination (origin) repo. PRs from any other repo are referenced
    as ``owner/repo#N`` — GitHub also auto-links those cross-repo refs.

    When ``needs_intervention`` is True, prepends a banner explaining that
    the branch holds the first ``failed_index`` cherry-picks of a group
    and that the PR at position ``failed_index`` could not be resolved
    automatically. The banner lists the conflicted files from the failed
    step (if known) and notes that any later PRs in the group were not
    attempted.
    """
    lines: list[str] = []
    if needs_intervention:
        applied = failed_index if failed_index is not None else 0
        remaining = max(0, len(unit.prs) - applied - 1)
        failed_ref = (
            pr_ref_label(failed_pr.repo_slug, failed_pr.number, origin_slug)
            if failed_pr is not None
            else "unknown"
        )
        lines.append(
            "> **This PR needs manual intervention.**"
        )
        lines.append(
            f"> Cherry-pick of {failed_ref} could not be resolved "
            "automatically (AI resolver was disabled, exhausted its "
            "iteration budget, or gave up)."
        )
        lines.append(
            f"> The branch contains the first {applied} commit(s) of "
            f"the group; {remaining} later PR(s) were not attempted."
        )
        if conflict_files:
            lines.append("> Conflicted files at the failure point:")
            for cf in conflict_files:
                lines.append(f"> - `{cf}`")
        lines.append(
            "> Resolve the conflict locally, push the fix, and mark this "
            "PR ready for review."
        )
        lines.append("")  # blank separator before the rest

    changelog = _build_changelog_block(unit)
    if changelog:
        lines.append(changelog)
        lines.append("")  # blank separator before the rest

    refs = [pr_ref_label(pr.repo_slug, pr.number, origin_slug) for pr in unit.prs]
    source_refs = ", ".join(refs)
    if unit.is_group or len(unit.prs) > 1:
        lines.append(
            f"Combined port of {len(unit.prs)} PR(s) "
            f"(group `{unit.group_id or unit.feature_id}`). "
            f"Cherry-picked from {source_refs}.\n"
        )
        for pr, ref in zip(unit.prs, refs):
            lines.append(f"- {ref} — {pr.title}")
        lines.append("")  # blank
        for pr, ref in zip(unit.prs, refs):
            if pr.body:
                lines.append(f"\n---\n### {ref}: {pr.title}\n\n{pr.body}")
    else:
        pr = unit.prs[0]
        lines.append(f"Cherry-picked from {source_refs}.")
        if pr.body:
            lines.append(f"\n---\n\n{pr.body}")
    return "\n".join(lines)


def _tag_commit_with_source_pr(
    repo_path: Path, unit: FeatureUnit, pr: PRInfo, origin_slug: str | None,
) -> None:
    """For grouped units, append a ``Source-PR`` trailer to the just-made
    commit so the combined PR's commit list is self-attributing.

    For singleton units this is skipped — the branch IS the source PR, so
    a trailer would be redundant noise on the commit.
    """
    if not unit.is_group or len(unit.prs) <= 1:
        return
    ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
    append_commit_trailer(
        repo_path, "Source-PR", f"{ref} ({pr.url})",
    )


def _cherry_pick_pr(
    repo_path: Path, config: Config, pr: PRInfo,
) -> OperationResult:
    """Cherry-pick one PR into the current branch.

    For PRs from the configured ``origin`` repo we use the origin remote
    (already fetched) and rely on the merge commit being locally present.
    For PRs from any other repo (cross-repo references in
    ``pr_sources.include_prs`` / ``pr_sources.groups[].prs``) we fetch the
    needed commit / PR ref directly from that repo's HTTPS URL — no extra
    git remote is added.
    """
    origin_slug = get_origin_repo_slug(config)
    is_external = origin_slug is None or pr.repo_slug != origin_slug
    fetch_target = (
        slug_to_https_url(pr.repo_slug) if is_external
        else config.origin.remote_name
    )

    if pr.state == "merged" and pr.merge_commit_sha:
        if is_external and not fetch_commit(
            repo_path, fetch_target, pr.merge_commit_sha,
        ):
            return OperationResult(
                success=False, conflict_files=[],
                error_message=(
                    f"could not fetch commit {pr.merge_commit_sha[:12]} "
                    f"from {pr.repo_slug}"
                ),
            )
        return cherry_pick_merge_commit(
            repo_path, pr.merge_commit_sha, abort_on_conflict=False,
        )

    if not fetch_pr_ref(repo_path, fetch_target, pr.number):
        return OperationResult(
            success=False, conflict_files=[],
            error_message=f"could not fetch PR #{pr.number} from {pr.repo_slug}",
        )
    return cherry_pick_merge_commit(
        repo_path, "FETCH_HEAD", abort_on_conflict=False,
    )


def _process_feature_unit(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    base_branch: str,
    base_ref: str,
    onto: str,
    remote: str,
    ai_active: bool,
) -> str:
    """Process one feature unit (single PR or sequential group).

    Always returns ``"continue"`` — unresolved conflicts are handled
    in-place by :func:`_handle_unresolved_conflict` (drop the local branch
    for singletons / first-of-group, or open a draft PR for partial
    groups), and the pipeline keeps moving.
    """
    origin_slug = get_origin_repo_slug(config)
    new_branch = config.feature_branch_name(unit.feature_id, onto)
    on_remote = remote_branch_exists(repo_path, new_branch, remote)
    on_local = local_branch_exists(repo_path, new_branch)
    label = (
        f"group {unit.group_id} ({len(unit.prs)} PRs)"
        if unit.is_group
        else (
            f"PR {pr_ref_label(unit.primary_pr().repo_slug, unit.primary_pr().number, origin_slug)}: "
            f"{unit.primary_pr().title}"
        )
    )

    if on_remote:
        console.print(
            f"\n    [cyan]{new_branch}[/cyan] ({label}) — already exists on "
            f"[cyan]{remote}[/cyan], skipping cherry-pick "
            "(resolve manually if you want to rebuild it)"
        )
        _ensure_pr_for_existing_remote_branch(
            config, state, unit, new_branch, base_branch,
        )
        return "continue"

    if on_local and unit.if_exists == "skip":
        console.print(
            f"\n    [cyan]{new_branch}[/cyan] ({label}) — local branch "
            "exists, skipping (set pr_sources.if_exists: recreate to rebuild)"
        )
        return "continue"

    if on_local:
        console.print(
            f"\n    [yellow]↻[/yellow] [cyan]{new_branch}[/cyan] exists "
            "locally, recreating from base"
        )

    desc = (
        unit.title_prefix or unit.group_id or unit.feature_id
        if unit.is_group
        else (
            f"{unit.title_prefix}{unit.primary_pr().title}"
            if unit.title_prefix else unit.primary_pr().title
        )
    )
    config.features.append(FeatureConfig(
        id=unit.feature_id, description=desc,
        source_branch="", enabled=True,
    ))

    console.print(f"\n  [cyan]{new_branch}[/cyan] ({label})")
    for pr in unit.prs:
        ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
        console.print(f"    PR {ref}: {pr.url}  [{pr.state}]")

    pr_meta = _unit_pr_meta(unit)

    stash_and_clean(repo_path)
    create_branch_from_ref(repo_path, new_branch, base_ref)

    # Cherry-pick each PR in order. Conflicts are handled per-PR; a
    # successful AI resolve continues to the next PR in the group.
    for idx, pr in enumerate(unit.prs):
        ref = pr_ref_label(pr.repo_slug, pr.number, origin_slug)
        if len(unit.prs) > 1:
            console.print(
                f"    [dim]→ cherry-picking {ref} "
                f"({idx + 1}/{len(unit.prs)})[/dim]"
            )
        cp_result = _cherry_pick_pr(repo_path, config, pr)

        if cp_result.success:
            _tag_commit_with_source_pr(repo_path, unit, pr, origin_slug)
            continue

        # --- Conflict path on this PR ---
        msg = f"Conflict on {ref}!"
        if cp_result.error_message and not cp_result.conflict_files:
            msg = f"{msg} ({cp_result.error_message})"
        console.print(f"    [red]✗[/red] {msg}")
        for cf in cp_result.conflict_files:
            console.print(f"      [red]•[/red] {cf}")

        handled = False
        if ai_active:
            handled = _try_ai_resolve_step(
                config, repo_path, unit, new_branch, base_branch, pr,
                cp_result.conflict_files,
            )

        if handled:
            _tag_commit_with_source_pr(repo_path, unit, pr, origin_slug)
            continue

        # --- Unhandled conflict — clean up and flag for manual review ---
        _handle_unresolved_conflict(
            config, repo_path, state, unit, new_branch, base_branch,
            base_ref, onto, idx, pr, cp_result.conflict_files, pr_meta,
            ai_attempted=ai_active,
        )
        return "continue"

    # --- All PRs cherry-picked cleanly (possibly via AI) ---
    _finish_clean_unit(
        config, repo_path, state, unit, new_branch, base_branch, onto, pr_meta,
    )
    return "continue"


def _success_status(rebase_pr_url: str | None) -> str:
    """Status for a port branch that finished cleanly (no conflicts).

    ``needs_review`` once a PR exists for the rebase branch — that's the
    "ready for human review" state. ``branch_created`` when the branch is
    around but no PR has been opened yet (e.g. ``pr_sources.auto_pr:
    false``, or PR creation hit a transient failure). The latter shows up
    on the project board with a branch link and a GitHub *compare* URL so
    the user can open the PR manually with one click.
    """
    return "needs_review" if rebase_pr_url else "branch_created"


def _ensure_pr_for_existing_remote_branch(
    config: Config,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
) -> None:
    """For a unit whose branch is already on origin: open (or find) the PR
    and update state / labels accordingly.

    Triggered on re-runs when the cherry-pick is skipped because the branch
    is already pushed (typical case: a prior run that ran with
    ``pr_sources.auto_pr: false`` and only pushed the branch). Idempotent:
    if a PR is already on file, this just makes sure the labels and project
    board reflect it.

    No-op unless ``config.push`` is enabled. When ``pr_sources.auto_pr``
    is off, the helper still ensures a state entry exists for the branch
    (status ``branch_created``) so the project board can show it with a
    compare-URL link.
    """
    if not config.push:
        return

    fs = state.features.get(unit.feature_id)
    if config.pr_sources.auto_pr:
        title = _unit_title(unit, config.project, base_branch)
        body = _unit_body(unit, get_origin_repo_slug(config))
        pr_url, outcome = _ensure_pr_for_branch(
            config, new_branch, base_branch, title, body,
        )
        _log_pr_action(outcome, pr_url)
    else:
        pr_url = None

    if fs is None:
        # Branch on remote but no state entry — record a minimal one so the
        # project board picks it up. Without prior context we can't tell
        # whether AI was involved; ``ai_resolved`` stays at its default
        # (False).
        fs = FeatureState(
            status=_success_status(pr_url), branch_name=new_branch,
            **_unit_pr_meta(unit),
        )
        state.features[unit.feature_id] = fs
    if pr_url:
        fs.rebase_pr_url = pr_url
        fs.status = "needs_review"
        _apply_releasy_label_to_pr(config, pr_url)
        if fs.ai_resolved:
            _apply_ai_label_to_pr(config, pr_url)
    _persist_state(config, state)


def _finish_clean_unit(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    onto: str,
    pr_meta: dict,
) -> None:
    """Push and (optionally) open a single combined PR for the unit.

    If any PR in the unit was AI-resolved, the resulting PR is tagged with
    ``ai_resolve.label`` and the feature state is marked accordingly.
    """
    ai_used = unit.ai_resolved_count > 0

    if config.push:
        _push(config, repo_path, new_branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
    else:
        console.print("    [dim]Skipping push[/dim]")

    # Provisional status — refined below once we know if a PR got opened.
    fs = FeatureState(
        status="branch_created" if config.push else "needs_review",
        branch_name=new_branch, base_commit=onto, **pr_meta,
    )
    if ai_used:
        fs.ai_resolved = True
        fs.ai_iterations = unit.ai_iterations_total or None
    if unit.ai_cost_usd_total is not None:
        fs.ai_cost_usd = unit.ai_cost_usd_total
    state.features[unit.feature_id] = fs

    if config.push and config.pr_sources.auto_pr:
        # Title format is identical regardless of AI involvement; the
        # `ai-resolved` label (applied below) is what marks it.
        title = _unit_title(unit, config.project, base_branch)
        rebase_pr_url, outcome = _ensure_pr_for_branch(
            config, new_branch, base_branch, title,
            _unit_body(unit, get_origin_repo_slug(config)),
        )
        _log_pr_action(outcome, rebase_pr_url)
        if rebase_pr_url:
            state.features[unit.feature_id].rebase_pr_url = rebase_pr_url
            state.features[unit.feature_id].status = "needs_review"
            _apply_releasy_label_to_pr(config, rebase_pr_url)
            if ai_used:
                _apply_ai_label_to_pr(config, rebase_pr_url)
    elif config.push and ai_used:
        # Branch pushed but pr_sources.auto_pr disabled — try to label any
        # pre-existing PR for this branch.
        existing = find_pr_for_branch(config, new_branch, base_branch)
        if existing:
            _apply_releasy_label_to_pr(
                config, existing.url, pr_number=existing.number,
            )
            _apply_ai_label_to_pr(config, existing.url, pr_number=existing.number)
            state.features[unit.feature_id].rebase_pr_url = existing.url
            state.features[unit.feature_id].status = "needs_review"

    _persist_state(config, state)


def _ensure_pr_for_branch(
    config: Config,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> tuple[str | None, str]:
    """Create a PR for ``branch`` or reuse / update an existing one.

    Behaviour:
      - If GitHub already has an open PR from ``branch`` → ``base_branch``:
          * with ``update_existing_prs: true`` in config, edit its title
            and body to match what releasy would have set, then return
            ``(url, "updated")``.
          * otherwise return ``(url, "reused")`` without touching the PR.
      - If no matching PR is open, create a new one and return
        ``(url, "created")``. On creation failure returns ``(None, "failed")``.
    """
    existing = find_pr_for_branch(config, branch, base_branch)
    if existing:
        if config.update_existing_prs:
            ok = update_pull_request(
                config, existing.number, title=title, body=body,
            )
            if ok:
                return existing.url, "updated"
            return existing.url, "reused"
        return existing.url, "reused"

    url = create_pull_request(config, branch, base_branch, title, body)
    if url:
        return url, "created"
    return None, "failed"


def _log_pr_action(outcome: str, url: str | None) -> None:
    """Pretty-print the result of ``_ensure_pr_for_branch``."""
    if outcome == "created" and url:
        console.print(
            f"    [green]✓[/green] PR opened: [link={url}]{url}[/link]"
        )
    elif outcome == "updated" and url:
        console.print(
            f"    [green]✓[/green] PR updated: [link={url}]{url}[/link]"
        )
    elif outcome == "reused" and url:
        console.print(
            f"    [dim]PR already open — left as-is: "
            f"[link={url}]{url}[/link] "
            f"(set [cyan]update_existing_prs: true[/cyan] to overwrite "
            f"title/body)[/dim]"
        )
    else:
        console.print(
            "    [yellow]![/yellow] Branch pushed but PR not created "
            "(see warnings above — common causes: PR already exists but "
            "was closed, or head/base have no difference)"
        )


def _apply_ai_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: add the ai_resolve.label to the PR identified by URL."""
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    ok = add_label_to_pr(config, pr_number, config.ai_resolve.label)
    if ok:
        console.print(
            f"    [magenta]🤖[/magenta] Labelled PR with "
            f"[magenta]{config.ai_resolve.label}[/magenta]"
        )
    else:
        console.print(
            f"    [yellow]![/yellow] Could not add label "
            f"'{config.ai_resolve.label}' to PR"
        )


def _apply_releasy_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: tag the PR with the ``releasy`` label.

    This is the replacement for the old ``[releasy]`` text prefix in the
    title — the label conveys the same identification, while keeping the
    title clean (``"Antalya 26.3: <subject>"``).
    """
    if pr_number is None:
        pr_number = _pr_number_from_url(pr_url)
    if pr_number is None:
        return
    add_label_to_pr(config, pr_number, RELEASY_LABEL)


def _handle_unresolved_conflict(
    config: Config,
    repo_path: Path,
    state: PipelineState,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    base_ref: str,
    onto: str,
    idx: int,
    failed_pr: PRInfo,
    conflict_files: list[str],
    pr_meta: dict,
    *,
    ai_attempted: bool,
) -> None:
    """Centralised cleanup for an unresolved cherry-pick conflict.

    Drops in two flavours, depending on whether earlier picks in the unit
    landed cleanly:

    * ``idx == 0`` (singleton, or the very first pick of a group): the
      branch has no commits worth keeping — abort the in-progress git op,
      detach from the branch, delete it locally, and record a
      ``conflict`` state entry with no PR / no push.

    * ``idx > 0`` (a partial group): the prior ``idx`` picks are valid
      commits — abort the current pick, push the branch as-is, open a
      DRAFT PR labelled ``ai-needs-attention`` with a banner explaining
      what failed, and record a ``conflict`` state entry pointing at the
      new draft PR. Remaining PRs in the group are NOT attempted.

    Either way the state is persisted (and synced to the GitHub Project
    when ``push`` is enabled) before returning, so the caller can simply
    move on to the next unit.
    """
    origin_slug = get_origin_repo_slug(config)
    ref = pr_ref_label(failed_pr.repo_slug, failed_pr.number, origin_slug)

    # 1. Make sure no git op is mid-flight. Idempotent: if the resolver
    # already aborted/reset (the AI path does this), these are no-ops.
    if is_operation_in_progress(repo_path):
        run_git(["cherry-pick", "--abort"], repo_path, check=False)
        run_git(["merge", "--abort"], repo_path, check=False)
        run_git(["rebase", "--abort"], repo_path, check=False)

    why = (
        "AI resolver gave up" if ai_attempted
        else "AI resolver disabled"
    )

    if idx == 0:
        # Nothing to keep — drop the branch entirely.
        if local_branch_exists(repo_path, new_branch):
            run_git(["checkout", "--detach", base_ref], repo_path, check=False)
            run_git(["branch", "-D", new_branch], repo_path, check=False)
        console.print(
            f"    [yellow]Dropped local branch[/yellow] [cyan]{new_branch}[/cyan] "
            f"({why}; nothing to keep)."
        )
        if unit.is_group:
            remaining = max(0, len(unit.prs) - 1)
            console.print(
                f"    [dim]Group {unit.group_id!r}: first PR {ref} could "
                f"not be resolved; {remaining} later PR(s) abandoned.[/dim]"
            )
        state.features[unit.feature_id] = FeatureState(
            status="conflict",
            branch_name=None,
            base_commit=onto,
            conflict_files=conflict_files,
            failed_step_index=idx,
            partial_pr_count=0,
            ai_cost_usd=unit.ai_cost_usd_total,
            **pr_meta,
        )
        _persist_state(config, state)
        return

    # Partial group: keep the n-1 successful commits, push, draft PR.
    applied = idx
    remaining = max(0, len(unit.prs) - applied - 1)
    console.print(
        f"    [yellow]Partial group:[/yellow] {applied} PR(s) applied, "
        f"{ref} unresolved ({why}); {remaining} later PR(s) abandoned."
    )

    rebase_pr_url: str | None = None
    pushed = False
    if config.push:
        _push(config, repo_path, new_branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
        pushed = True

    if pushed and config.pr_sources.auto_pr:
        title = _unit_title(unit, config.project, base_branch)
        body = _unit_body(
            unit,
            origin_slug,
            needs_intervention=True,
            failed_index=applied,
            failed_pr=failed_pr,
            conflict_files=conflict_files,
        )
        rebase_pr_url = create_pull_request(
            config, new_branch, base_branch, title, body,
            draft=True,
            labels=[config.ai_resolve.needs_attention_label],
        )
        if rebase_pr_url:
            console.print(
                f"    [green]✓[/green] Draft PR opened: "
                f"[link={rebase_pr_url}]{rebase_pr_url}[/link] "
                f"[dim](label: {config.ai_resolve.needs_attention_label})[/dim]"
            )
            _apply_releasy_label_to_pr(config, rebase_pr_url)
        else:
            console.print(
                "    [yellow]![/yellow] Could not open draft PR for "
                f"[cyan]{new_branch}[/cyan] (see warnings above)"
            )

    fs = FeatureState(
        status="conflict",
        branch_name=new_branch,
        base_commit=onto,
        conflict_files=conflict_files,
        failed_step_index=applied,
        partial_pr_count=applied,
        ai_cost_usd=unit.ai_cost_usd_total,
        **pr_meta,
    )
    if rebase_pr_url:
        fs.rebase_pr_url = rebase_pr_url
    state.features[unit.feature_id] = fs
    _persist_state(config, state)


def _try_ai_resolve_step(
    config: Config,
    repo_path: Path,
    unit: FeatureUnit,
    new_branch: str,
    base_branch: str,
    pr: PRInfo,
    conflict_files: list[str],
) -> bool:
    """Invoke Claude to resolve ONE conflicted cherry-pick step in place.

    Step-mode contract: on success Claude has resolved, built, and committed
    locally — the cherry-pick is concluded, the working tree is clean, and
    HEAD has advanced. RelEasy stays in charge of pushing the branch and
    opening the (possibly combined) PR. This contract is the same for
    singletons and for any step inside a sequential group.

    Returns True on success. On failure the working tree is reset to a
    clean state at ``start_sha`` (handled inside ``attempt_ai_resolve``)
    and the caller is responsible for deciding what to do next — typically
    routing through :func:`_handle_unresolved_conflict`.
    """
    from releasy.ai_resolve import AIResolveContext, attempt_ai_resolve

    ctx = AIResolveContext(
        port_branch=new_branch,
        base_branch=base_branch,
        source_pr=pr,
        conflict_files=conflict_files,
        operation="cherry-pick",
    )

    result = attempt_ai_resolve(config, repo_path, ctx)

    # Cost is billed even when Claude failed — record it before deciding
    # what to do about the failure.
    if result.cost_usd is not None:
        unit.ai_cost_usd_total = (
            (unit.ai_cost_usd_total or 0.0) + result.cost_usd
        )

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
        return False

    unit.ai_resolved_count += 1
    if result.iterations:
        unit.ai_iterations_total += result.iterations
    iters = (
        f" (iterations: {result.iterations})" if result.iterations else ""
    )
    cost = (
        f" [dim](cost: ${result.cost_usd:.4f})[/dim]"
        if result.cost_usd is not None else ""
    )
    console.print(
        f"    [green]✓[/green] AI resolved #{pr.number}{iters}{cost}"
    )
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
    state = load_state(config)
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

    state.features[feat.id].status = _success_status(
        state.features[feat.id].rebase_pr_url
    )
    state.features[feat.id].conflict_files = []
    _persist_state(config, state)
    console.print(
        f"[green]✓[/green] Feature [cyan]{feat.id}[/cyan] "
        f"({fs.branch_name}) → {state.features[feat.id].status}"
    )
    _reconcile_project_board(config, state)
    return True


def _branch_resolution_state(
    repo_path: Path, branch: str, base_ref: str,
) -> tuple[bool, str | None]:
    """Inspect a port branch and decide whether it has been resolved.

    Returns ``(resolved, reason_if_not)``. Resolved means: branch is
    checked out cleanly, no unmerged files, no in-progress cherry-pick,
    and HEAD has at least one commit beyond ``base_ref``.
    """
    co = run_git(["checkout", branch], repo_path, check=False)
    if co.returncode != 0:
        return False, "could not checkout branch (uncommitted changes elsewhere?)"

    if is_operation_in_progress(repo_path):
        return False, "cherry-pick/merge/rebase still in progress"

    unmerged = run_git(["ls-files", "--unmerged"], repo_path, check=False)
    if unmerged.stdout.strip():
        files = sorted({line.split("\t", 1)[1] for line in unmerged.stdout.splitlines()})
        return False, "unmerged files: " + ", ".join(files)

    porc = run_git(["status", "--porcelain"], repo_path, check=False)
    if porc.stdout.strip():
        return False, "working tree has uncommitted changes"

    cnt = run_git(
        ["rev-list", "--count", f"{base_ref}..{branch}"], repo_path, check=False,
    )
    if cnt.returncode != 0 or cnt.stdout.strip() == "0":
        return False, f"branch has no commits beyond {base_ref}"

    return True, None


def _open_pr_for_resolved(
    config: Config, repo_path: Path, state: PipelineState, fs: FeatureState,
    base_branch: str,
) -> None:
    """Push and open a PR for an already-resolved port branch."""
    branch = fs.branch_name
    assert branch is not None

    if not config.push:
        console.print("    [dim]push disabled — branch left local[/dim]")
        return

    if remote_branch_exists(repo_path, branch, config.origin.remote_name):
        console.print(
            f"    [dim]already on origin, not force-pushing[/dim]"
        )
    else:
        _push(config, repo_path, branch)
        console.print(f"    [green]✓[/green] Pushed [cyan]{branch}[/cyan]")

    subject = (
        _strip_misleading_title_prefix(fs.pr_title, config.project)
        if fs.pr_title else branch
    )
    prefix = _subject_prefix(config.project, base_branch)
    title = f"{prefix}: {subject}" if prefix else subject

    body_parts: list[str] = []
    origin_slug = get_origin_repo_slug(config)
    pr_urls = fs.pr_urls or ([fs.pr_url] if fs.pr_url else [])
    refs: list[str] = []
    for url in pr_urls:
        parsed = parse_pr_url(url) if url else None
        if parsed:
            owner, repo, n = parsed
            refs.append(pr_ref_label(f"{owner}/{repo}", n, origin_slug))
    # Fallback if old state lacks pr_urls but does have pr_numbers (assume origin).
    if not refs:
        pr_numbers = fs.pr_numbers or ([fs.pr_number] if fs.pr_number else [])
        refs = [f"#{n}" for n in pr_numbers if n is not None]
    if refs:
        body_parts.append(f"Cherry-picked from {', '.join(refs)}.")
    if fs.pr_body:
        body_parts.append(f"\n---\n\n{fs.pr_body}")
    body = "\n".join(body_parts) or branch

    if fs.rebase_pr_url:
        pr_num = _pr_number_from_url(fs.rebase_pr_url)
        if config.update_existing_prs:
            if pr_num is not None and update_pull_request(
                config, pr_num, title=title, body=body,
            ):
                console.print(
                    f"    [green]✓[/green] PR updated: "
                    f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link]"
                )
            else:
                console.print(
                    f"    [yellow]![/yellow] Could not update PR "
                    f"{fs.rebase_pr_url}"
                )
        else:
            console.print(
                f"    [dim]PR already opened — left as-is: "
                f"[link={fs.rebase_pr_url}]{fs.rebase_pr_url}[/link] "
                f"(set [cyan]update_existing_prs: true[/cyan] to overwrite "
                f"title/body)[/dim]"
            )
        # Make sure the `releasy` label is present even on PRs from older
        # runs that predated label-based identification.
        _apply_releasy_label_to_pr(config, fs.rebase_pr_url, pr_number=pr_num)
        if fs.ai_resolved:
            _apply_ai_label_to_pr(
                config, fs.rebase_pr_url, pr_number=pr_num,
            )
        return

    remote_base = f"{config.origin.remote_name}/{base_branch}"
    remote_head = f"{config.origin.remote_name}/{branch}"
    ahead = run_git(
        ["rev-list", "--count", f"{remote_base}..{remote_head}"],
        repo_path, check=False,
    )
    if ahead.returncode == 0:
        try:
            ahead_n = int(ahead.stdout.strip())
        except ValueError:
            ahead_n = -1
        if ahead_n == 0:
            console.print(
                f"    [yellow]![/yellow] Branch has no commits ahead of "
                f"[cyan]{base_branch}[/cyan] — skipping PR creation "
                f"(stale branch from an earlier run? delete it with "
                f"[cyan]git push {config.origin.remote_name} "
                f":{branch}[/cyan])"
            )
            return

    pr_url, outcome = _ensure_pr_for_branch(
        config, branch, base_branch, title, body,
    )
    _log_pr_action(outcome, pr_url)
    if pr_url:
        fs.rebase_pr_url = pr_url
        fs.status = "needs_review"
        state.features[_feature_id_from_branch(state, branch)] = fs
        _apply_releasy_label_to_pr(config, pr_url)
        if fs.ai_resolved:
            _apply_ai_label_to_pr(config, pr_url)


def _pr_number_from_url(url: str) -> int | None:
    """Parse the trailing ``/pull/<N>`` segment of a PR URL."""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def _feature_id_from_branch(state: PipelineState, branch: str) -> str:
    for fid, fs in state.features.items():
        if fs.branch_name == branch:
            return fid
    return branch


def continue_all(config: Config, work_dir: Path | None = None) -> bool:
    """Re-check every feature in state and finish whatever can be finished.

    This is the catch-all "reconcile everything" command. Per feature:

      - ``skipped`` → log and skip.
      - ``conflict`` from an AI-gave-up partial group / dropped singleton
        (any of ``failed_step_index`` / ``partial_pr_count`` /
        ``rebase_pr_url`` set) → highlight; user must act on the draft
        PR or source PR, then re-run.
      - ``conflict``, branch now clean → push, open PR (if ``auto_pr``),
        flip to ``needs_review`` or ``branch_created``.
      - ``conflict``, still unresolved → highlight, leave alone.
      - ``branch_created`` (branch on origin, no PR yet) → try to open
        the PR. Covers the case where the previous run had
        ``pr_sources.auto_pr: false`` and only pushed the branch, or
        where an earlier failure prevented PR creation. Stays as
        ``branch_created`` if PR creation is still disabled / failing.
      - ``needs_review`` already linked to a PR → leave alone.

    Always finishes with a project-board reconciliation pass so the GitHub
    Project reflects the current state (and stale draft stubs get replaced
    by the real PR cards).
    """
    state = load_state(config)
    if not state.features:
        console.print(
            "[yellow]No features in state. Run 'releasy run' first.[/yellow]"
        )
        return False

    if _prune_superseded_singletons(config, state):
        _persist_state(config, state)

    repo_path = _setup_repo(config, work_dir, state.base_branch)

    if is_operation_in_progress(repo_path):
        console.print(
            f"\n[red]✗[/red] A git operation is still in progress in "
            f"[cyan]{repo_path}[/cyan]."
        )
        console.print(
            "  Finish (`git cherry-pick --continue`) or abort it first, "
            "then re-run."
        )
        return False

    base_branch = state.base_branch or (
        config.base_branch_name(state.onto or "") if state.onto else None
    )
    if not base_branch:
        console.print(
            "[red]Cannot determine base branch from state.[/red] Run "
            "'releasy run' first."
        )
        return False
    base_ref = f"{config.origin.remote_name}/{base_branch}"
    remote_name = config.origin.remote_name

    console.print(
        f"\n[bold]Continuing[/bold] — base [cyan]{base_branch}[/cyan]"
    )

    any_unresolved = False
    for feat_id, fs in state.features.items():
        branch = fs.branch_name or feat_id
        header = f"\n  [cyan]{branch}[/cyan]"

        if fs.status == "skipped":
            console.print(f"{header} — [dim]skipped[/dim]")
            continue

        # AI-gave-up flavour of conflict (partial group / dropped
        # singleton) — these have an explicit human-action checkpoint
        # (the draft PR or the source PR), so we never auto-flip them
        # below; the user re-runs ``continue`` after the manual fix.
        if fs.status == "conflict" and (
            fs.failed_step_index is not None
            or fs.partial_pr_count is not None
            or fs.rebase_pr_url
        ):
            console.print(
                f"{header} — [dim]conflict (AI gave up)[/dim] "
                "— fix locally / on the draft PR, then re-run"
            )
            continue

        # Already-finished states. ``needs_review`` is terminal (PR exists);
        # ``branch_created`` is the "branch pushed but no PR" case, where
        # ``releasy continue`` will try to open the PR.
        if fs.status == "needs_review":
            console.print(
                f"{header} — [dim]needs-review, PR open[/dim]"
            )
            continue
        if fs.status == "branch_created":
            if not (config.push and config.pr_sources.auto_pr):
                console.print(
                    f"{header} — [dim]branch-created (auto_pr off, "
                    "open PR manually)[/dim]"
                )
                continue
            if not fs.branch_name or not (
                local_branch_exists(repo_path, fs.branch_name)
                or remote_branch_exists(repo_path, fs.branch_name, remote_name)
            ):
                console.print(
                    f"{header} [yellow]branch missing (local & remote), "
                    "skipping[/yellow]"
                )
                continue
            console.print(
                f"{header} — [green]branch-created[/green], opening PR"
            )
            _open_pr_for_resolved(config, repo_path, state, fs, base_branch)
            _persist_state(config, state)
            continue

        # Conflict path needs the branch locally so we can inspect / continue.
        if not fs.branch_name or not local_branch_exists(repo_path, fs.branch_name):
            console.print(
                f"{header} [yellow]branch missing locally, skipping[/yellow]"
            )
            continue

        if fs.status != "conflict":
            console.print(f"{header} — [dim]status {fs.status}, skipping[/dim]")
            continue

        resolved, reason = _branch_resolution_state(
            repo_path, fs.branch_name, base_ref,
        )
        if not resolved:
            any_unresolved = True
            console.print(f"{header} [red]✗ still unresolved[/red] — {reason}")
            if fs.conflict_files:
                for cf in fs.conflict_files:
                    console.print(f"      [red]•[/red] {cf}")
            console.print(
                f"      [dim]cd {repo_path} && git status     # then resolve, "
                "git add -A && git cherry-pick --continue[/dim]"
            )
            continue

        console.print(f"{header} [green]✓ resolved[/green]")
        fs.conflict_files = []
        # Provisional — flips to needs_review inside _open_pr_for_resolved
        # if a PR is opened (or already exists for the branch).
        fs.status = _success_status(fs.rebase_pr_url)
        state.features[feat_id] = fs
        _open_pr_for_resolved(config, repo_path, state, fs, base_branch)
        _persist_state(config, state)

    _reconcile_project_board(config, state)

    if any_unresolved:
        console.print(
            "\n[yellow]Some ports still have unresolved conflicts (see above). "
            "Fix them and re-run [bold]releasy continue[/bold].[/yellow]"
        )
        return False

    console.print("\n[green]All ports processed.[/green]")
    return True


def sync_to_project(config: Config) -> bool:
    """Standalone reconciliation: push current local state to the board.

    Loads the per-project state file and calls the same reconciliation
    used at the end of ``releasy continue``, so the user can refresh the
    project board without running the whole pipeline (handy after editing
    state by hand, after rotating tokens, or right after wiring up a new
    project URL on an in-flight rebase).

    Returns False — for non-zero CLI exit — only when the user asked for a
    sync but nothing happened: no project configured, missing token,
    unparseable URL, or sync errors. A clean "already up to date" is
    success.
    """
    if not config.notifications.github_project:
        console.print(
            "[yellow]No GitHub Project configured.[/yellow] Set "
            "[cyan]notifications.github_project[/cyan] in config.yaml or "
            "run [cyan]releasy setup-project[/cyan] first."
        )
        return False

    state = load_state(config)
    if not state.features and not config.features:
        console.print(
            "[yellow]Nothing to sync.[/yellow] No features in state and "
            "no static features in config — run [cyan]releasy run[/cyan] "
            "first."
        )
        return False

    console.print(
        f"\n[bold]Syncing local state[/bold] → "
        f"[cyan]{config.notifications.github_project}[/cyan]"
    )
    summary = sync_project(config, state)

    if summary.skipped:
        console.print(
            f"  [yellow]project sync skipped:[/yellow] {summary.skipped_reason}"
        )
        return False
    if summary.added:
        console.print(
            f"  [green]✓[/green] added {summary.added} missing item(s) "
            "to the project board"
        )
    if summary.updated:
        console.print(
            f"  [dim]refreshed {summary.updated} existing card(s)[/dim]"
        )
    if not summary.added and not summary.updated and not summary.errors:
        console.print("  [dim]project board already up to date[/dim]")
    if summary.errors:
        console.print(
            f"  [yellow]![/yellow] {summary.errors} item(s) could not be "
            "synced — see warnings above"
        )
        return False
    return True


def _reconcile_project_board(config: Config, state: PipelineState) -> None:
    """Make sure every local port is reflected on the GitHub Project board.

    Per-feature state changes during the run already trigger
    ``sync_project`` from ``_persist_state`` (when ``push`` is
    on). This is the belt-and-braces pass: even with ``push: false``, or
    when the project URL was added to config after some ports were
    already in state, we still want ``releasy continue`` to leave the
    board in sync with what we have locally.

    Output is a single, friendly line — quiet when there's nothing to do,
    informative when there is.
    """
    if not config.notifications.github_project:
        return
    console.print("\n[dim]Reconciling GitHub Project board...[/dim]")
    summary = sync_project(config, state)
    if summary.skipped:
        console.print(
            f"  [yellow]project sync skipped:[/yellow] {summary.skipped_reason}"
        )
        return
    if summary.added and summary.errors == 0:
        console.print(
            f"  [green]✓[/green] added {summary.added} missing item(s) to "
            "the project board"
        )
    if summary.updated:
        console.print(
            f"  [dim]refreshed {summary.updated} existing card(s)[/dim]"
        )
    if not summary.added and not summary.updated and not summary.errors:
        console.print("  [dim]project board already up to date[/dim]")
    if summary.errors:
        console.print(
            f"  [yellow]![/yellow] {summary.errors} item(s) could not be "
            "synced — see warnings above"
        )


def skip_branch(config: Config, branch_name: str) -> bool:
    """Mark a port branch as skipped."""
    state = load_state(config)
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
    _persist_state(config, state)
    console.print(f"[yellow]⏭[/yellow] Feature [cyan]{feat.id}[/cyan] skipped")
    return True


def abort_run(config: Config) -> None:
    """Abort the current run, leaving all branches as-is."""
    state = load_state(config)
    console.print("[yellow]Aborting current run. All branches left as-is.[/yellow]")
    _persist_state(config, state)


def print_status(config: Config) -> None:
    """Print the current pipeline state, grouped by status.

    One sub-table per status section (in :data:`STATUS_DISPLAY_ORDER`),
    so the most-attention-needing entries (conflicts) surface at the top.
    """
    from rich.table import Table
    from releasy.state import STATUS_DISPLAY_ORDER
    from releasy.status import STATUS_HEADINGS, STATUS_ICONS

    state = load_state(config)

    console.print()
    console.print(
        f"Last run: {state.started_at or 'N/A'}  ·  "
        f"Onto: {state.onto or 'N/A'}  ·  "
        f"Phase: {state.phase}"
    )
    if state.base_branch:
        console.print(f"Base branch: [cyan]{state.base_branch}[/cyan]")

    section_styles = {
        "needs_review": "blue", "branch_created": "yellow",
        "conflict": "red", "skipped": "yellow",
    }

    origin_slug = get_origin_repo_slug(config)

    def _ai_cell(fs: FeatureState) -> str:
        if not fs.ai_resolved:
            return ""
        iters = f" ({fs.ai_iterations}×)" if fs.ai_iterations else ""
        return f"[magenta]ai-resolved[/magenta]{iters}"

    def _pr_cell(fs: FeatureState) -> str:
        if not fs.rebase_pr_url:
            return ""
        label = "PR"
        if fs.pr_url:
            parsed = parse_pr_url(fs.pr_url)
            if parsed:
                owner, repo, n = parsed
                label = pr_ref_label(f"{owner}/{repo}", n, origin_slug)
        elif fs.pr_number:
            label = f"#{fs.pr_number}"
        return f"[link={fs.rebase_pr_url}]{label}[/link]"

    if not state.features:
        console.print("\n[dim]No ports tracked yet.[/dim]")
        return

    by_status: dict[str, list[tuple[str, FeatureState]]] = {}
    for fid, fs in state.features.items():
        by_status.setdefault(fs.status, []).append((fid, fs))

    ordered = [s for s in STATUS_DISPLAY_ORDER if s in by_status]
    ordered.extend(sorted(s for s in by_status if s not in STATUS_DISPLAY_ORDER))

    summary_parts = [
        f"{len(by_status[s])} {STATUS_ICONS.get(s, s)}"
        for s in ordered
    ]
    console.print(f"\n[bold]Summary:[/bold] {'  ·  '.join(summary_parts)}")

    for status in ordered:
        rows = by_status[status]
        style = section_styles.get(status, "white")
        heading = STATUS_HEADINGS.get(status, status)
        icon = STATUS_ICONS.get(status, status)
        table = Table(
            title=f"[{style}]{icon} — {heading} ({len(rows)})[/{style}]",
            title_justify="left",
            show_header=True,
        )
        table.add_column("Branch", style="cyan")
        table.add_column("AI", style="magenta")
        table.add_column("Based On")
        table.add_column("Source PR")
        table.add_column("Rebase PR")
        if status == "conflict":
            table.add_column("Conflict Files", style="red")
        for fid, fs in rows:
            feat = next((f for f in config.features if f.id == fid), None)
            label = fs.branch_name or (feat.source_branch if feat else None) or fid
            source_pr = ""
            if fs.pr_url:
                parsed = parse_pr_url(fs.pr_url)
                if parsed:
                    owner, repo, n = parsed
                    source_pr = (
                        f"[link={fs.pr_url}]"
                        f"{pr_ref_label(f'{owner}/{repo}', n, origin_slug)}"
                        f"[/link]"
                    )
                elif fs.pr_number:
                    source_pr = f"[link={fs.pr_url}]#{fs.pr_number}[/link]"
            row = [
                label,
                _ai_cell(fs),
                (fs.base_commit or "")[:12],
                source_pr,
                _pr_cell(fs),
            ]
            if status == "conflict":
                row.append(", ".join(fs.conflict_files))
            table.add_row(*row)
        console.print()
        console.print(table)

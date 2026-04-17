"""Port-cherry-pick pipeline.

Assumes the base branch (e.g. ``antalya-26.3``) already exists on origin.
For each source PR discovered via configured labels, the pipeline creates a
port branch off ``origin/<base_branch>``, cherry-picks the PR merge commit,
and on conflict either invokes the AI resolver or commits WIP markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from releasy.config import Config, FeatureConfig, PRGroupConfig, PRSourceConfig
from releasy.git_ops import (
    abort_in_progress_op,
    append_commit_trailer,
    branch_exists,
    local_branch_exists,
    remote_branch_exists,
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
    update_submodules,
)
from releasy.github_ops import (
    PRInfo,
    add_label_to_pr,
    commit_and_push_state,
    create_pull_request,
    ensure_label,
    fetch_pr_by_number,
    find_pr_for_branch,
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


def _setup_repo(
    config: Config, work_dir: Path | None, base_branch: str | None = None,
) -> Path:
    """Set up work repo and fetch origin. Upstream is not fetched anymore —
    the ``onto`` argument is used only for branch naming.

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
    """Push a branch to the origin remote, with upstream safety check."""
    force_push(
        repo_path, branch, config.origin.remote_name,
        upstream_name=config.upstream.remote_name if config.upstream else None,
    )


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
    auto_pr: bool
    title_prefix: str = ""        # used for both single-PR and group titles
    is_group: bool = False
    group_id: str | None = None   # filled when is_group
    # Mutable per-run bookkeeping (set by _process_feature_unit):
    ai_resolved_count: int = 0
    ai_iterations_total: int = 0

    @property
    def sort_key(self) -> tuple[str, int]:
        """Earliest merged_at across constituent PRs, fallback to PR number."""
        merged = [pr.merged_at for pr in self.prs if pr.merged_at]
        first = min(merged) if merged else "9999"
        return (first, min(pr.number for pr in self.prs))

    def primary_pr(self) -> PRInfo:
        return self.prs[0]


def _build_singleton_units(
    config: Config,
    collected: dict[int, tuple[PRInfo, PRSourceConfig]],
) -> list[FeatureUnit]:
    units: list[FeatureUnit] = []
    for pr, src in collected.values():
        units.append(FeatureUnit(
            feature_id=f"pr-{pr.number}",
            prs=[pr],
            if_exists=src.if_exists,
            auto_pr=src.auto_pr,
            title_prefix=src.description,
        ))
    return units


def _build_group_units(
    config: Config,
    excluded_pr_numbers: set[int],
    excluded_labels: set[str],
) -> tuple[list[FeatureUnit], set[int]]:
    """Materialise group units, fetching each PR. Returns (units, claimed_nums)
    where ``claimed_nums`` are PR numbers that should NOT also appear as
    standalone units.
    """
    units: list[FeatureUnit] = []
    claimed: set[int] = set()
    for group in config.pr_sources.groups:
        console.print(
            f"\n  Resolving group [yellow]{group.id}[/yellow] "
            f"({len(group.prs)} PR(s))"
        )
        group_prs: list[PRInfo] = []
        for url in group.prs:
            num = parse_pr_url(url)
            if num is None:
                console.print(f"    [red]✗[/red] Bad PR URL: {url}")
                continue
            if num in excluded_pr_numbers:
                console.print(
                    f"    [yellow]−[/yellow] #{num} excluded via "
                    "pr_sources.exclude_prs, dropping from group"
                )
                continue
            pr = fetch_pr_by_number(config, num)
            if pr is None:
                console.print(f"    [red]✗[/red] Could not fetch PR #{num}")
                continue
            if excluded_labels and (set(pr.labels) & excluded_labels):
                console.print(
                    f"    [yellow]−[/yellow] #{num} carries an excluded label, "
                    "dropping from group"
                )
                continue
            group_prs.append(pr)
            claimed.add(num)
            console.print(
                f"    [dim]+ #{num}[/dim] {pr.title} [{pr.state}]"
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
            auto_pr=group.auto_pr,
            title_prefix=group.description,
            is_group=True,
            group_id=group.id,
        ))
    return units, claimed


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
            else PRSourceConfig(labels=[], if_exists=config.pr_sources.if_exists)
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

    # --- Build sequential PR groups (each becomes one combined feature) ---
    excluded_label_set = set(prs_cfg.exclude_labels)
    group_units, claimed_pr_numbers = _build_group_units(
        config, exclude_pr_numbers, excluded_label_set,
    )
    # Drop any singletons that the groups have claimed.
    for n in claimed_pr_numbers:
        if n in collected:
            pr_info, _ = collected.pop(n)
            console.print(
                f"  [dim]#{n} ({pr_info.title}) belongs to a group — "
                "removed from singletons[/dim]"
            )

    units: list[FeatureUnit] = (
        _build_singleton_units(config, collected) + group_units
    )
    units.sort(key=lambda u: u.sort_key)

    if not units:
        console.print("\n  [dim]No PRs or groups to process after filtering[/dim]")

    existing_ids = {f.id for f in config.features}
    for unit in units:
        if unit.feature_id in existing_ids:
            continue
        existing_ids.add(unit.feature_id)
        outcome = _process_feature_unit(
            config, repo_path, state, unit, base_branch, base_ref, onto,
            remote, ai_active,
        )
        if outcome == "stop":
            break

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


def _unit_pr_meta(unit: FeatureUnit) -> dict:
    """Build state-meta dict for a unit (single PR or group)."""
    primary = unit.primary_pr()
    return {
        "pr_url": primary.url,
        "pr_number": primary.number,
        "pr_title": primary.title,
        "pr_body": primary.body,
        "pr_numbers": [pr.number for pr in unit.prs],
        "pr_urls": [pr.url for pr in unit.prs],
    }


def _unit_title(unit: FeatureUnit) -> str:
    """Synthesise the [releasy]-prefixed PR title for a unit."""
    if unit.is_group:
        if unit.title_prefix:
            return f"[releasy] {unit.title_prefix.rstrip()}".strip()
        n = len(unit.prs)
        first_title = unit.prs[0].title
        if n == 1:
            return f"[releasy] {first_title}"
        return f"[releasy] {unit.group_id}: combined port of {n} PRs"
    pr = unit.primary_pr()
    title = pr.title
    if unit.title_prefix:
        title = f"{unit.title_prefix}{title}"
    return f"[releasy] {title}"


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


def _unit_body(unit: FeatureUnit, conflicted: bool = False) -> str:
    """Build the PR body listing constituent PRs."""
    lines: list[str] = []
    if conflicted:
        lines.append("> **WARNING:** Unresolved conflict markers.\n")

    changelog = _build_changelog_block(unit)
    if changelog:
        lines.append(changelog)
        lines.append("")  # blank separator before the rest

    source_refs = ", ".join(f"#{pr.number}" for pr in unit.prs)
    if unit.is_group or len(unit.prs) > 1:
        lines.append(
            f"Combined port of {len(unit.prs)} PR(s) "
            f"(group `{unit.group_id or unit.feature_id}`). "
            f"Cherry-picked from {source_refs}.\n"
        )
        for pr in unit.prs:
            lines.append(f"- #{pr.number} — {pr.title}")
        lines.append("")  # blank
        for pr in unit.prs:
            if pr.body:
                lines.append(f"\n---\n### #{pr.number}: {pr.title}\n\n{pr.body}")
    else:
        pr = unit.prs[0]
        lines.append(f"Cherry-picked from {source_refs}.")
        if pr.body:
            lines.append(f"\n---\n\n{pr.body}")
    return "\n".join(lines)


def _tag_commit_with_source_pr(
    repo_path: Path, unit: FeatureUnit, pr: PRInfo,
) -> None:
    """For grouped units, append a ``Source-PR`` trailer to the just-made
    commit so the combined PR's commit list is self-attributing.

    For singleton units this is skipped — the branch IS the source PR, so
    a trailer would be redundant noise on the commit.
    """
    if not unit.is_group or len(unit.prs) <= 1:
        return
    append_commit_trailer(
        repo_path, "Source-PR", f"#{pr.number} ({pr.url})",
    )


def _cherry_pick_pr(
    repo_path: Path, remote: str, pr: PRInfo,
):
    """Cherry-pick one PR into the current branch."""
    if pr.state == "merged" and pr.merge_commit_sha:
        return cherry_pick_merge_commit(
            repo_path, pr.merge_commit_sha, abort_on_conflict=False,
        )
    if not fetch_pr_ref(repo_path, remote, pr.number):
        from releasy.git_ops import CherryPickResult
        return CherryPickResult(
            success=False, conflict_files=[], error=f"could not fetch PR #{pr.number}",
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

    Returns ``"stop"`` if the pipeline should halt (unresolved conflict, no
    WIP fallback), or ``"continue"`` otherwise.
    """
    new_branch = config.feature_branch_name(unit.feature_id, onto)
    on_remote = remote_branch_exists(repo_path, new_branch, remote)
    on_local = local_branch_exists(repo_path, new_branch)
    label = (
        f"group {unit.group_id} ({len(unit.prs)} PRs)"
        if unit.is_group
        else f"PR #{unit.primary_pr().number}: {unit.primary_pr().title}"
    )

    if on_remote:
        console.print(
            f"\n    [cyan]{new_branch}[/cyan] ({label}) — already exists on "
            f"[cyan]{remote}[/cyan], skipping (resolve manually if you want "
            "to rebuild it)"
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
        console.print(f"    PR #{pr.number}: {pr.url}  [{pr.state}]")

    pr_meta = _unit_pr_meta(unit)

    stash_and_clean(repo_path)
    create_branch_from_ref(repo_path, new_branch, base_ref)

    # Cherry-pick each PR in order. Conflicts are handled per-PR; a
    # successful AI resolve continues to the next PR in the group.
    for idx, pr in enumerate(unit.prs):
        if len(unit.prs) > 1:
            console.print(
                f"    [dim]→ cherry-picking #{pr.number} "
                f"({idx + 1}/{len(unit.prs)})[/dim]"
            )
        cp_result = _cherry_pick_pr(repo_path, remote, pr)

        if cp_result.success:
            _tag_commit_with_source_pr(repo_path, unit, pr)
            continue

        # --- Conflict path on this PR ---
        console.print(f"    [red]✗[/red] Conflict on #{pr.number}!")
        for cf in cp_result.conflict_files:
            console.print(f"      [red]•[/red] {cf}")

        handled = False
        if ai_active:
            handled = _try_ai_resolve_step(
                config, repo_path, unit, new_branch, base_branch, pr,
                cp_result.conflict_files,
            )

        if handled:
            _tag_commit_with_source_pr(repo_path, unit, pr)
            continue

        # --- Unhandled conflict — record state and decide whether to halt ---
        state.features[unit.feature_id] = FeatureState(
            status="conflict", branch_name=new_branch, base_commit=onto,
            conflict_files=cp_result.conflict_files, **pr_meta,
        )

        if not config.wip_commit_on_conflict:
            console.print(
                "\n    [yellow]Pipeline stopped on unresolved conflict.[/yellow]"
            )
            console.print(
                f"    Branch [cyan]{new_branch}[/cyan] left with conflict "
                "markers and an in-progress cherry-pick."
            )
            if unit.is_group:
                remaining = len(unit.prs) - idx - 1
                if remaining > 0:
                    console.print(
                        f"    [dim]Group {unit.group_id!r}: {idx} PR(s) "
                        f"applied, {remaining} not yet attempted.[/dim]"
                    )
            console.print("\n    To resolve manually:")
            console.print(f"      [dim]cd {repo_path}[/dim]")
            console.print("      [dim]# edit the conflicted files, then:[/dim]")
            console.print(
                "      [dim]git add -A && git cherry-pick --continue[/dim]"
            )
            if unit.is_group and idx < len(unit.prs) - 1:
                rest = " ".join(
                    str(p.merge_commit_sha or f"PR#{p.number}")
                    for p in unit.prs[idx + 1:]
                )
                console.print(
                    f"      [dim]# then cherry-pick the remaining PR(s):"
                    f"\n      git cherry-pick {rest}[/dim]"
                )
            console.print(
                f"      [dim]releasy continue --branch {new_branch}[/dim]"
            )
            console.print(
                "      [dim]releasy run    # to continue with remaining ports[/dim]\n"
            )
            _update_state_and_status(config, state)
            return "stop"

        # WIP-marker fallback: commit markers, push, open PR, give up on group.
        commit_conflict_markers(repo_path)
        console.print("    [yellow]↑[/yellow] Committed conflict markers as WIP")
        if config.push:
            _push(config, repo_path, new_branch)
            console.print(f"    [green]✓[/green] Pushed [cyan]{new_branch}[/cyan]")
        if config.push and unit.auto_pr:
            rebase_pr_url = create_pull_request(
                config, new_branch, base_branch,
                _unit_title(unit), _unit_body(unit, conflicted=True),
            )
            if rebase_pr_url:
                state.features[unit.feature_id].rebase_pr_url = rebase_pr_url
                console.print(
                    f"    [green]✓[/green] PR opened: [link={rebase_pr_url}]"
                    f"{rebase_pr_url}[/link]"
                )
        _update_state_and_status(config, state)
        return "continue"

    # --- All PRs cherry-picked cleanly (possibly via AI) ---
    _finish_clean_unit(
        config, repo_path, state, unit, new_branch, base_branch, onto, pr_meta,
    )
    return "continue"


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

    status = "resolved" if ai_used else "ok"
    fs = FeatureState(
        status=status, branch_name=new_branch, base_commit=onto, **pr_meta,
    )
    if ai_used:
        fs.ai_resolved = True
        fs.ai_iterations = unit.ai_iterations_total or None
    state.features[unit.feature_id] = fs

    if config.push and unit.auto_pr:
        title = _unit_title(unit)
        if ai_used:
            title = title.replace("[releasy]", "[releasy] ai-resolved:", 1)
        rebase_pr_url = create_pull_request(
            config, new_branch, base_branch, title, _unit_body(unit),
        )
        if rebase_pr_url:
            state.features[unit.feature_id].rebase_pr_url = rebase_pr_url
            console.print(
                f"    [green]✓[/green] PR opened: [link={rebase_pr_url}]"
                f"{rebase_pr_url}[/link]"
            )
            if ai_used:
                _apply_ai_label_to_pr(config, rebase_pr_url)
        else:
            console.print(
                "    [yellow]![/yellow] Branch pushed but PR not created "
                "(see warnings above)"
            )
    elif config.push and ai_used:
        # Branch pushed but no auto_pr — try to label any pre-existing PR.
        existing = find_pr_for_branch(config, new_branch, base_branch)
        if existing:
            _apply_ai_label_to_pr(config, existing.url, pr_number=existing.number)
            state.features[unit.feature_id].rebase_pr_url = existing.url

    _update_state_and_status(config, state)


def _apply_ai_label_to_pr(
    config: Config, pr_url: str, pr_number: int | None = None,
) -> None:
    """Best-effort: add the ai_resolve.label to the PR identified by URL."""
    if pr_number is None:
        # Extract trailing /pull/<n>
        try:
            pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
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

    Returns True on success. On failure the caller decides whether to halt
    (default) or fall through to the WIP-marker path.
    """
    from releasy.ai_resolve import AIResolveContext, resolve_with_claude

    head = run_git(["rev-parse", "--verify", "HEAD"], repo_path, check=False)
    start_sha = head.stdout.strip() if head.returncode == 0 else None

    ctx = AIResolveContext(
        port_branch=new_branch,
        base_branch=base_branch,
        source_pr=pr,
        conflict_files=conflict_files,
        start_sha=start_sha,
    )

    result = resolve_with_claude(config, repo_path, ctx)

    if not result.success:
        reason = result.error or (
            "timed out" if result.timed_out else "unknown failure"
        )
        console.print(f"    [yellow]AI resolve failed:[/yellow] {reason}")
        # Reset to a known state so the user sees the original conflict and
        # can resolve it manually. If Claude left the cherry-pick aborted but
        # committed something half-way, hard-reset back to start_sha.
        if is_operation_in_progress(repo_path):
            run_git(["cherry-pick", "--abort"], repo_path, check=False)
            run_git(["merge", "--abort"], repo_path, check=False)
            run_git(["rebase", "--abort"], repo_path, check=False)
        if start_sha:
            run_git(["reset", "--hard", start_sha], repo_path, check=False)
        # Re-apply the conflict so the human can resolve from a clean state.
        if pr.merge_commit_sha:
            cherry_pick_merge_commit(
                repo_path, pr.merge_commit_sha, abort_on_conflict=False,
            )
        else:
            cherry_pick_merge_commit(
                repo_path, "FETCH_HEAD", abort_on_conflict=False,
            )
        return False

    unit.ai_resolved_count += 1
    if result.iterations:
        unit.ai_iterations_total += result.iterations
    iters = (
        f" (iterations: {result.iterations})" if result.iterations else ""
    )
    console.print(
        f"    [green]✓[/green] AI resolved #{pr.number}{iters}"
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

    if fs.rebase_pr_url:
        console.print(
            f"    [dim]PR already opened: {fs.rebase_pr_url}[/dim]"
        )
        return

    existing = find_pr_for_branch(config, branch, base_branch)
    if existing:
        fs.rebase_pr_url = existing.url
        state.features[_feature_id_from_branch(state, branch)] = fs
        console.print(
            f"    [dim]PR already exists on GitHub — reusing: "
            f"[link={existing.url}]{existing.url}[/link][/dim]"
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

    title_src = fs.pr_title or branch
    body_parts = []
    pr_numbers = fs.pr_numbers or ([fs.pr_number] if fs.pr_number else [])
    if pr_numbers:
        source_refs = ", ".join(f"#{n}" for n in pr_numbers)
        body_parts.append(f"Cherry-picked from {source_refs}.")
    if fs.pr_body:
        body_parts.append(f"\n---\n\n{fs.pr_body}")

    pr_url = create_pull_request(
        config, branch, base_branch,
        f"[releasy] {title_src}", "\n".join(body_parts) or branch,
    )
    if pr_url:
        fs.rebase_pr_url = pr_url
        state.features[_feature_id_from_branch(state, branch)] = fs
        console.print(
            f"    [green]✓[/green] PR opened: [link={pr_url}]{pr_url}[/link]"
        )
    else:
        console.print(
            "    [yellow]![/yellow] Branch pushed but PR not created "
            "(see warnings above — common causes: PR already exists but "
            "was closed, or head/base have no difference)"
        )


def _feature_id_from_branch(state: PipelineState, branch: str) -> str:
    for fid, fs in state.features.items():
        if fs.branch_name == branch:
            return fid
    return branch


def continue_all(config: Config, work_dir: Path | None = None) -> bool:
    """Process every feature in state and either finalise it or flag it.

    Decisions per feature:
      - status ok / skipped / disabled → log and skip
      - status conflict, branch now clean → mark resolved, push, open PR
      - status conflict, still unresolved → highlight, leave alone
      - status resolved without PR → push, open PR
    """
    state = load_state(config.repo_dir)
    if not state.features:
        console.print(
            "[yellow]No features in state. Run 'releasy run' first.[/yellow]"
        )
        return False

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

    console.print(
        f"\n[bold]Continuing[/bold] — base [cyan]{base_branch}[/cyan]"
    )

    any_unresolved = False
    for feat_id, fs in state.features.items():
        branch = fs.branch_name or feat_id
        header = f"\n  [cyan]{branch}[/cyan]"

        if fs.status in ("ok", "skipped", "disabled"):
            console.print(f"{header} — [dim]{fs.status}, skipping[/dim]")
            continue

        if not fs.branch_name or not local_branch_exists(repo_path, fs.branch_name):
            console.print(
                f"{header} [yellow]branch missing locally, skipping[/yellow]"
            )
            continue

        if fs.status == "resolved":
            console.print(f"{header} — already resolved")
            _open_pr_for_resolved(config, repo_path, state, fs, base_branch)
            _update_state_and_status(config, state)
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
        fs.status = "resolved"
        fs.conflict_files = []
        state.features[feat_id] = fs
        _open_pr_for_resolved(config, repo_path, state, fs, base_branch)
        _update_state_and_status(config, state)

    if any_unresolved:
        console.print(
            "\n[yellow]Some ports still have unresolved conflicts (see above). "
            "Fix them and re-run [bold]releasy continue[/bold].[/yellow]"
        )
        return False

    console.print("\n[green]All ports processed.[/green]")
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

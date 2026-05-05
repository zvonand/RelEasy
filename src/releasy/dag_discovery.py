"""Auto-discover a PR dependency DAG and emit a recommended grouping.

The ``releasy discover-deps`` command:

1. Walks the candidate PR set defined by ``config.pr_sources``, treating
   each user-declared group as a single super-node.
2. Excludes units already merged into the target branch (state.yaml +
   ``Source-PR:`` trailers + ``git cherry``).
3. Trial-cherry-picks each remaining unit onto the target tip in a
   scratch git worktree (``git worktree add --detach``).
4. On a clean pick, emits the unit as a leaf in the DAG.
5. On conflict, looks up older un-ported units that touched the
   conflicting files (via ``git log target..source -- <file>`` mapped
   through merge-commit / Source-PR: trailer / merge-containment rules),
   then optionally hands the candidates to Claude to confirm.
6. After all units are processed, computes weakly-connected components
   → the recommended groups, with articulation points called out as
   ``recommend_first``.

The command is read-only: it never writes ``state.yaml`` and never
touches the main worktree. The optional ``--write-session`` flag writes
a sidecar overlay at ``<session-stem>.auto-deps.yaml`` that the session
loader merges into ``pr_sources.groups`` on the next ``releasy run``.
"""

from __future__ import annotations

import atexit
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from releasy.ai_resolve import (
    _MISSING_PREREQS_RE,
    _parse_missing_prereqs,
    synthesize_text,
)
from releasy.config import Config, _auto_deps_overlay_path
from releasy.git_ops import (
    abort_in_progress_op,
    cherry_pick_merge_commit,
    ensure_work_repo,
    fetch_remote,
    get_conflict_files,
    is_operation_in_progress,
    run_git,
)
from releasy.github_ops import PRInfo, get_origin_repo_slug
from releasy.pipeline import (
    FeatureUnit,
    _SOURCE_PR_URL_RE,
    discover_feature_units,
)
from releasy.state import PipelineState, load_state
from releasy.termlog import get_console

console = get_console()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class _PickOutcome:
    clean: bool
    conflict_files: list[str]
    error_message: str | None = None


@dataclass
class _CandidateUnit:
    """A unit (singleton or user-declared group) under consideration.

    Wraps a :class:`FeatureUnit` with bookkeeping fields that only matter
    during dep discovery (e.g. earliest merge timestamp for the latest →
    oldest queue order).
    """
    unit_id: str
    is_user_group: bool
    prs: list[PRInfo]
    earliest_merged_at: str | None
    feature_unit: FeatureUnit  # Backing FeatureUnit, used for cherry-pick order


@dataclass
class DAGNode:
    unit_id: str
    is_user_group: bool
    pr_urls: list[str]
    pr_titles: list[str]
    earliest_merged_at: str | None
    deps: list[str]
    discovery_method: str  # "trial-clean" | "git-graph" | "git-graph+claude"
    conflict_files_at_discovery: list[str] = field(default_factory=list)


@dataclass
class DAGComponent:
    component_id: str
    unit_ids: list[str]
    recommend_first: list[str]
    edges: list[tuple[str, str]]


@dataclass
class DiscoveryReport:
    base_branch: str
    target_sha: str
    generated_at: str
    candidate_unit_count: int
    skipped_already_in_target: list[str]
    nodes: list[DAGNode]
    components: list[DAGComponent]
    singletons: list[str]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_discover_deps(
    config: Config,
    onto: str | None,
    work_dir: Path | None,
    *,
    output_path: Path | None,
    write_session: bool,
    use_ai: bool,
    max_depth: int,
    pr_limit: int | None,
    include_already_merged: bool,
) -> DiscoveryReport:
    """Run dep discovery and write the report (and optionally the sidecar).

    Returns the in-memory :class:`DiscoveryReport`. The caller is expected
    to print a summary; the YAML output(s) are written here as a side effect.
    """
    # --- Resolve target branch + scratch worktree ---
    if onto:
        base_branch = config.base_branch_name(onto)
    elif config.target_branch:
        base_branch = config.target_branch
    else:
        raise ValueError(
            "discover-deps: cannot resolve base branch — pass --onto or set "
            "target_branch in config.yaml."
        )

    wd = config.resolve_work_dir(work_dir)
    repo_path, _ = ensure_work_repo(config, wd)
    if is_operation_in_progress(repo_path):
        raise RuntimeError(
            f"main repo {repo_path} has an in-progress git op (cherry-pick "
            f"/ merge / rebase) — finish or abort it first, then re-run "
            "discover-deps."
        )
    # Scratch parent is always the user-blessed work_dir. Plan §6:
    # ``<work_dir>/.releasy-discover-deps-<short_id>``. We deliberately
    # don't use ``repo_path.parent`` because ``ensure_work_repo`` returns
    # ``repo_path == work_dir`` when work_dir already has a ``.git``, in
    # which case ``repo_path.parent`` would be the user's home directory.
    scratch_parent = wd
    scratch_parent.mkdir(parents=True, exist_ok=True)

    remote = config.origin.remote_name
    fetch_remote(repo_path, remote)
    target_ref = f"{remote}/{base_branch}"
    target_sha = _resolve_sha(repo_path, target_ref)

    # --- Build candidate units ---
    units = discover_feature_units(config)
    candidates = _build_candidate_unit_set(units, config)
    if pr_limit is not None and len(candidates) > pr_limit:
        candidates = candidates[-pr_limit:]  # most-recent N (sorted newest-last)

    warnings_acc: list[str] = []
    candidate_pr_urls = {p.url for cu in candidates for p in cu.prs}

    # --- Detect already-merged units ---
    state = load_state(config)
    state_already = _state_already_in_target(candidates, state)
    trailer_already = _trailer_scan(repo_path, target_ref, candidate_pr_urls)
    cherry_already = _git_cherry_already(
        repo_path, target_ref, candidates, warnings_acc,
    )
    pr_in_target: set[str] = state_already | trailer_already | cherry_already

    fully_merged_units: set[str] = set()
    for cu in candidates:
        if all(p.url in pr_in_target for p in cu.prs):
            fully_merged_units.add(cu.unit_id)

    # ``include_already_merged`` only changes the *report* (already-merged
    # units are appended as zero-edge nodes near the end of the function);
    # the trial-pick traversal always operates on the active set, since
    # there is nothing to learn from re-picking an already-applied PR.
    active_for_traversal = [
        cu for cu in candidates if cu.unit_id not in fully_merged_units
    ]

    # Build pr_url → unit_id and merge_sha → unit_id indices for unit
    # projection in the conflict-mapping step. Filter merge SHAs to those
    # actually present in the local object DB — cross-repo PRs from
    # ``include_prs`` typically aren't fetched, and including their SHAs
    # in ``git log --not target_ref <shas...>`` makes git error out and
    # drop the whole file's classification.
    pr_url_to_unit: dict[str, str] = {}
    merge_sha_to_unit: dict[str, str] = {}
    skipped_remote_sha: list[str] = []
    for cu in candidates:
        for p in cu.prs:
            pr_url_to_unit[p.url] = cu.unit_id
            if not p.merge_commit_sha:
                continue
            chk = run_git(
                ["cat-file", "-e", p.merge_commit_sha],
                repo_path, check=False,
            )
            if chk.returncode == 0:
                merge_sha_to_unit[p.merge_commit_sha] = cu.unit_id
            else:
                skipped_remote_sha.append(p.url)
    if skipped_remote_sha:
        warnings_acc.append(
            f"{len(skipped_remote_sha)} PR merge commit(s) not present "
            "locally (cross-repo / unfetched); excluded from conflict "
            "classification — these units will only appear as candidate "
            "deps when their unit_id is referenced directly"
        )

    # --- Run trial picks in scratch worktree ---
    nodes: dict[str, DAGNode] = {}
    edges: set[tuple[str, str]] = set()
    by_unit_id: dict[str, _CandidateUnit] = {cu.unit_id: cu for cu in candidates}
    merge_containment_cache: dict[str, str] | None = None

    scratch = _open_scratch_worktree(repo_path, scratch_parent, target_ref)
    try:
        # Process latest-merged-at first (descending). Recursion into older
        # candidates is pushed onto the queue.
        queue: list[tuple[str, int]] = [
            (cu.unit_id, 0)
            for cu in sorted(
                active_for_traversal,
                key=lambda c: (c.earliest_merged_at or "0000", c.prs[0].number),
                reverse=True,
            )
        ]

        while queue:
            unit_id, depth = queue.pop(0)
            if unit_id in nodes:
                continue
            cu = by_unit_id.get(unit_id)
            if cu is None:
                # An edge pointed at a unit that isn't in the candidate set
                # (or is fully merged). Caller's filtering should have
                # prevented this; warn and continue.
                warnings_acc.append(
                    f"unit {unit_id!r} referenced as a dep but not in candidate set; skipping"
                )
                continue
            if depth > max_depth:
                warnings_acc.append(
                    f"unit {unit_id!r} hit max-depth={max_depth}; transitive "
                    "deps may be incomplete"
                )
                # Still record the node so edges pointing at it resolve.
                # Use a distinct ``discovery_method`` so the YAML reader
                # can tell "we never trial-picked this" from "we picked
                # it and it was clean".
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="depth-cutoff",
                    conflict_files=[],
                )
                continue

            outcome = _trial_pick_unit(scratch, cu, target_ref)
            if outcome.clean:
                nodes[unit_id] = _make_node(
                    cu, deps=[], method="trial-clean", conflict_files=[],
                )
                continue

            if outcome.error_message and not outcome.conflict_files:
                # Trial-pick failed without producing conflict markers —
                # almost always "merge_commit_sha is None" or a git error.
                # Surface it so the user isn't left wondering why this
                # unit shows up with no deps.
                warnings_acc.append(
                    f"unit {unit_id!r}: trial pick failed without "
                    f"conflict files: {outcome.error_message}"
                )

            # Conflict path — find candidate-deps via git history
            if merge_containment_cache is None:
                merge_containment_cache = _build_merge_containment_map(
                    repo_path, target_ref, candidates, warnings_acc,
                )
            cand_dep_unit_ids = _candidate_deps_for_conflict(
                scratch, target_ref, outcome.conflict_files,
                candidate_merge_shas=list(merge_sha_to_unit.keys()),
                merge_sha_to_unit=merge_sha_to_unit,
                pr_url_to_unit=pr_url_to_unit,
                merge_containment=merge_containment_cache,
                exclude_unit_ids={unit_id},
                already_in_target_units=fully_merged_units,
            )

            method = "git-graph"
            if use_ai and cand_dep_unit_ids:
                confirmed = _ask_claude_for_prereqs(
                    config, cu, outcome.conflict_files, cand_dep_unit_ids,
                    by_unit_id, base_branch, warnings_acc,
                )
                if confirmed is not None:
                    cand_dep_unit_ids = confirmed
                    method = "git-graph+claude"

            # Drop dep references to unit IDs not in the candidate set —
            # this happens when ``--limit`` truncated the universe,
            # leaving an out-of-limit unit visible in the conflict map
            # but never trial-picked. The component-build at line 354
            # already silently ignores edges referencing non-nodes;
            # filtering here keeps ``report.nodes[].deps`` consistent
            # with the eventual ``components`` list.
            dropped_deps: list[str] = []
            deps: list[str] = []
            for d in cand_dep_unit_ids:
                if d in by_unit_id:
                    deps.append(d)
                else:
                    dropped_deps.append(d)
            if dropped_deps:
                warnings_acc.append(
                    f"unit {unit_id!r}: dropped {len(dropped_deps)} dep "
                    f"reference(s) outside the candidate set: "
                    f"{', '.join(dropped_deps)} — likely truncated by "
                    "--limit or already-merged exclusion"
                )
            nodes[unit_id] = _make_node(
                cu, deps=deps, method=method,
                conflict_files=outcome.conflict_files,
            )
            for d in deps:
                edges.add((unit_id, d))
                if d not in nodes:
                    queue.append((d, depth + 1))
    finally:
        _close_scratch_worktree(repo_path, scratch)

    # --- Break spurious cycles (older→newer wins) ---
    edges = _break_cycles(edges, by_unit_id, warnings_acc)

    # User-declared groups whose discovery showed deps deserve to be
    # called out. We don't auto-mutate the main session — the user owns
    # those entries — but we do tell them via a warning so the
    # recommendation isn't buried inside ``nodes[].deps``.
    for cu in candidates:
        if cu.is_user_group:
            node = nodes.get(cu.unit_id)
            if node and node.deps:
                warnings_acc.append(
                    f"user group {cu.unit_id!r} depends on: "
                    f"{', '.join(node.deps)} — "
                    "add these to its `depends_on:` in the session file "
                    "if you want sequential gating in `releasy run`"
                )

    # --- Compute weakly-connected components and articulation points ---
    components, singletons = _components(nodes, edges, by_unit_id)

    # --- Build report ---
    skipped = sorted(fully_merged_units)
    if include_already_merged:
        for uid in skipped:
            if uid not in nodes:
                cu = by_unit_id[uid]
                nodes[uid] = _make_node(
                    cu, deps=[], method="trial-clean",
                    conflict_files=[],
                )

    report = DiscoveryReport(
        base_branch=base_branch,
        target_sha=target_sha,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        candidate_unit_count=len(candidates),
        skipped_already_in_target=skipped,
        nodes=sorted(nodes.values(), key=_node_sort_key),
        components=components,
        singletons=singletons,
        warnings=warnings_acc,
    )

    # --- Write outputs ---
    # Session overlay first so any "session_path is unknown" warning it
    # appends lands in the diagnostic report's on-disk YAML (the
    # ``warnings`` list is the same object). Any failure writing the
    # overlay is captured as a warning rather than propagated, so the
    # diagnostic report — the durable artifact — always lands.
    if write_session:
        session_path = (
            config.session.session_path
            if config.session and config.session.session_path
            else None
        )
        if session_path is None:
            warnings_acc.append(
                "--write-session: session_path is unknown; skipping overlay write"
            )
        else:
            overlay_path = _auto_deps_overlay_path(session_path)
            try:
                _write_session_overlay(report, overlay_path)
            except OSError as e:
                warnings_acc.append(
                    f"--write-session: failed to write overlay "
                    f"{overlay_path}: {e}"
                )
            else:
                console.print(
                    f"  [green]✓[/green] wrote auto-deps overlay → "
                    f"[cyan]{overlay_path}[/cyan]"
                )

    output_path = output_path or _default_report_path(config, base_branch)
    _write_report(report, output_path)

    return report


# ---------------------------------------------------------------------------
# Candidate set + already-merged detection
# ---------------------------------------------------------------------------


def _build_candidate_unit_set(
    units: list[FeatureUnit], config: Config,
) -> list[_CandidateUnit]:
    """Flatten ``discover_feature_units`` output into _CandidateUnit's.

    Sort by earliest merged_at ascending (oldest first); the traversal
    later iterates this in reverse for the "latest → oldest" walk.
    """
    out: list[_CandidateUnit] = []
    for u in units:
        merged = [p.merged_at for p in u.prs if p.merged_at]
        earliest = min(merged) if merged else None
        out.append(_CandidateUnit(
            unit_id=u.feature_id,
            is_user_group=u.is_group,
            prs=list(u.prs),
            earliest_merged_at=earliest,
            feature_unit=u,
        ))
    out.sort(key=lambda c: (
        c.earliest_merged_at or "9999",
        c.prs[0].number if c.prs else 0,
    ))
    return out


def _state_already_in_target(
    candidates: list[_CandidateUnit], state: PipelineState,
) -> set[str]:
    """PR URLs whose unit is already recorded as merged/branch_created in state."""
    out: set[str] = set()
    state_urls: set[str] = set()
    for fs in state.features.values():
        if fs.status in ("merged",):
            if fs.pr_url:
                state_urls.add(fs.pr_url)
            for u in fs.pr_urls or []:
                state_urls.add(u)
    for cu in candidates:
        for p in cu.prs:
            if p.url in state_urls:
                out.add(p.url)
    return out


def _trailer_scan(
    repo_path: Path, target_ref: str, candidate_urls: set[str],
) -> set[str]:
    """Scan target's recent history for ``Source-PR:`` trailers; return matched URLs."""
    if not candidate_urls:
        return set()
    rev_range = f"{target_ref}~2000..{target_ref}"
    # If target has fewer than 2000 commits, fall back to full history.
    result = run_git(
        ["log", rev_range,
         "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)"],
        repo_path, check=False,
    )
    if result.returncode != 0:
        result = run_git(
            ["log", target_ref,
             "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)"],
            repo_path, check=False,
        )
    if result.returncode != 0:
        return set()
    out: set[str] = set()
    for line in result.stdout.splitlines():
        for m in _SOURCE_PR_URL_RE.finditer(line):
            url = m.group(0)
            if url in candidate_urls:
                out.add(url)
    return out


def _git_cherry_already(
    repo_path: Path, target_ref: str,
    candidates: list[_CandidateUnit], warnings_acc: list[str],
) -> set[str]:
    """For each candidate PR's merge_commit_sha, ask ``git cherry`` whether
    its patch-id is already applied in target.

    We don't have one canonical "source ref" because PRs may come from
    different repos / branches; instead we run ``git cherry`` per
    candidate merge SHA against target_ref. Cheap (single rev-parse
    each), and tolerant of missing objects (skipped silently).
    """
    out: set[str] = set()
    for cu in candidates:
        for p in cu.prs:
            sha = p.merge_commit_sha
            if not sha:
                continue
            # Verify the SHA is present locally; otherwise skip — cross-repo
            # PRs from include_prs may not have been fetched.
            check = run_git(
                ["cat-file", "-e", sha], repo_path, check=False,
            )
            if check.returncode != 0:
                continue
            cherry = run_git(
                ["cherry", target_ref, sha], repo_path, check=False,
            )
            if cherry.returncode != 0:
                continue
            for line in cherry.stdout.splitlines():
                line = line.strip()
                if line.startswith("- "):
                    out.add(p.url)
                    break
    return out


# ---------------------------------------------------------------------------
# Trial cherry-pick environment
# ---------------------------------------------------------------------------


def _open_scratch_worktree(
    repo_path: Path, scratch_parent: Path, target_ref: str,
) -> Path:
    """Create a detached scratch worktree at ``target_ref`` under
    ``scratch_parent`` and register a best-effort cleanup via
    :mod:`atexit` (in addition to the caller's try/finally).

    The parent is the user-blessed work_dir, never derived from
    ``repo_path.parent`` (see :func:`run_discover_deps` for the rationale).
    """
    # Reuse the project's standard short-id helper so scratch dirs sort
    # alongside other releasy-managed names.
    from releasy.cli import _short_id

    short_id = _short_id()
    scratch = scratch_parent / f".releasy-discover-deps-{short_id}"
    run_git(
        ["worktree", "add", "--detach", str(scratch), target_ref],
        repo_path,
    )

    cleaned = [False]

    def _cleanup() -> None:
        if cleaned[0]:
            return
        cleaned[0] = True
        try:
            run_git(
                ["worktree", "remove", "--force", str(scratch)],
                repo_path, check=False,
            )
        except Exception:  # pragma: no cover — best-effort cleanup
            pass
    atexit.register(_cleanup)
    # Stash the cleaned flag on the path object so the explicit close
    # path can mark it done and avoid a duplicate call at process exit.
    scratch._releasy_cleanup_flag = cleaned  # type: ignore[attr-defined]
    return scratch


def _close_scratch_worktree(repo_path: Path, scratch: Path) -> None:
    flag = getattr(scratch, "_releasy_cleanup_flag", None)
    if flag is not None:
        flag[0] = True
    run_git(
        ["worktree", "remove", "--force", str(scratch)],
        repo_path, check=False,
    )


def _trial_pick_unit(
    scratch: Path, unit: _CandidateUnit, target_ref: str,
) -> _PickOutcome:
    """Sequentially cherry-pick every PR in the unit. Returns clean iff
    every PR applied. On the first conflict, captures the conflict files
    and exits the loop (subsequent PRs in the unit are not attempted in
    this trial). Always resets after.
    """
    prs = _ordered_prs_for_pick(unit)
    try:
        for p in prs:
            sha = p.merge_commit_sha
            if not sha:
                # Unmerged or cross-repo PR with no merge SHA — skip the
                # unit; record an empty-conflict outcome with a synthetic
                # marker so the caller surfaces it as a warning.
                return _PickOutcome(
                    clean=False, conflict_files=[],
                    error_message=f"PR {p.url} has no merge_commit_sha",
                )
            res = cherry_pick_merge_commit(
                scratch, sha, abort_on_conflict=False,
            )
            if not res.success:
                return _PickOutcome(
                    clean=False,
                    conflict_files=list(res.conflict_files),
                    error_message=res.error_message,
                )
        return _PickOutcome(clean=True, conflict_files=[])
    finally:
        # Reset after EVERY trial, clean or otherwise. Cheap.
        abort_in_progress_op(scratch)
        run_git(["reset", "--hard", target_ref], scratch, check=False)
        run_git(["clean", "-fdx"], scratch, check=False)


def _ordered_prs_for_pick(unit: _CandidateUnit) -> list[PRInfo]:
    """Return the unit's PRs in cherry-pick order — same logic
    :func:`_build_group_units` uses (group.sort honoured).
    """
    fu = unit.feature_unit
    if fu.is_group:
        # The FeatureUnit was already sorted in _build_group_units when
        # group.sort == "merged_at". For "listed", keep current order.
        return list(fu.prs)
    return list(fu.prs)


# ---------------------------------------------------------------------------
# Conflict file → candidate unit mapping
# ---------------------------------------------------------------------------


def _build_merge_containment_map(
    repo_path: Path, target_ref: str,
    candidates: list[_CandidateUnit], warnings_acc: list[str],
) -> dict[str, str]:
    """Return ``{non_merge_sha: enclosing_merge_sha}`` for commits between
    target_ref and any candidate merge commit's first-parent diff.

    Only candidate merge commits matter — drift commits don't get
    classified anyway. Built once per discover-deps run.
    """
    containment: dict[str, str] = {}
    for cu in candidates:
        for p in cu.prs:
            mc = p.merge_commit_sha
            if not mc:
                continue
            # Verify object exists locally
            chk = run_git(["cat-file", "-e", mc], repo_path, check=False)
            if chk.returncode != 0:
                continue
            # Get the merge's parents
            parents_res = run_git(
                ["rev-list", "--parents", "-n", "1", mc],
                repo_path, check=False,
            )
            if parents_res.returncode != 0 or not parents_res.stdout.strip():
                continue
            parts = parents_res.stdout.strip().split()
            if len(parts) < 3:
                # Not a merge commit (only 1 parent) — skip.
                continue
            p1, p2 = parts[1], parts[2]
            log_res = run_git(
                ["log", "--format=%H", f"{p1}..{p2}"],
                repo_path, check=False,
            )
            if log_res.returncode != 0:
                continue
            for sha in log_res.stdout.split():
                containment.setdefault(sha, mc)
    return containment


def _candidate_deps_for_conflict(
    repo_path: Path,
    target_ref: str,
    conflict_files: list[str],
    *,
    candidate_merge_shas: list[str],
    merge_sha_to_unit: dict[str, str],
    pr_url_to_unit: dict[str, str],
    merge_containment: dict[str, str],
    exclude_unit_ids: set[str],
    already_in_target_units: set[str],
) -> list[str]:
    """Map conflict files back to candidate unit IDs that touched them.

    Algorithm: for each conflict file, run ``git log --not target_ref
    <merge_shas...> -- file`` to enumerate commits reachable from any
    candidate but not target that touched the file. Classify each commit
    via merge-commit match → Source-PR trailer → containment map. Project
    PR URLs to unit IDs. Drop the trial-pick's own unit and units already
    in target.
    """
    if not conflict_files or not candidate_merge_shas:
        return []
    cand_set = set(candidate_merge_shas)
    found_units: list[str] = []
    seen: set[str] = set()
    for f in conflict_files:
        log_args = ["log", "--format=%H", "--not", target_ref] + list(cand_set) + ["--", f]
        try:
            res = run_git(log_args, repo_path, check=False)
        except Exception:
            continue
        if res.returncode != 0:
            continue
        for sha in res.stdout.split():
            unit_id = _classify_commit_to_unit(
                repo_path, sha, candidate_merge_shas=cand_set,
                merge_sha_to_unit=merge_sha_to_unit,
                pr_url_to_unit=pr_url_to_unit,
                merge_containment=merge_containment,
            )
            if not unit_id:
                continue
            if unit_id in exclude_unit_ids:
                continue
            if unit_id in already_in_target_units:
                continue
            if unit_id in seen:
                continue
            seen.add(unit_id)
            found_units.append(unit_id)
    return found_units


def _classify_commit_to_unit(
    repo_path: Path,
    sha: str,
    *,
    candidate_merge_shas: set[str],
    merge_sha_to_unit: dict[str, str],
    pr_url_to_unit: dict[str, str],
    merge_containment: dict[str, str],
) -> str | None:
    """Three-rule precedence:
    1. ``sha`` is a candidate's merge_commit_sha → direct lookup.
    2. Commit carries a ``Source-PR:`` trailer matching a candidate URL.
    3. Commit is contained in one of the candidate merge commits (merge_containment).
    """
    # Rule 1: direct merge match
    if sha in candidate_merge_shas:
        uid = merge_sha_to_unit.get(sha)
        if uid is not None:
            return uid

    # Rule 2: Source-PR trailer
    show = run_git(
        ["show", "-s",
         "--format=%(trailers:key=Source-PR,unfold=true,valueonly=true)",
         sha],
        repo_path, check=False,
    )
    if show.returncode == 0 and show.stdout.strip():
        for line in show.stdout.splitlines():
            for m in _SOURCE_PR_URL_RE.finditer(line):
                url = m.group(0)
                if url in pr_url_to_unit:
                    return pr_url_to_unit[url]

    # Rule 3: containment in a candidate merge commit
    enclosing = merge_containment.get(sha)
    if enclosing:
        uid = merge_sha_to_unit.get(enclosing)
        if uid is not None:
            return uid

    return None


# ---------------------------------------------------------------------------
# Claude integration
# ---------------------------------------------------------------------------


def _ask_claude_for_prereqs(
    config: Config,
    unit: _CandidateUnit,
    conflict_files: list[str],
    candidate_unit_ids: list[str],
    by_unit_id: dict[str, _CandidateUnit],
    base_branch: str,
    warnings_acc: list[str],
) -> list[str] | None:
    """Ask Claude to confirm/refine the deterministic candidate-deps list.

    Renders ``prompts/discover_prereqs.md`` and parses the model's
    ``MISSING_PREREQS:`` output. Returns the confirmed subset (URLs
    mapped back to unit_ids) or ``None`` to signal "AI unavailable, use
    deterministic candidates as-is".
    """
    prompt_path = (
        config.config_path.parent / "prompts" / "discover_prereqs.md"
    )
    if not prompt_path.exists():
        # Fallback to bundled template
        prompt_path = (
            Path(__file__).parent / "prompts" / "discover_prereqs.md"
        )
    if not prompt_path.exists():
        warnings_acc.append(
            "discover_prereqs.md prompt template not found; "
            "skipping Claude refinement"
        )
        return None

    template = prompt_path.read_text(encoding="utf-8")

    cand_block_lines: list[str] = []
    url_to_unit: dict[str, str] = {}
    for cuid in candidate_unit_ids:
        cu = by_unit_id.get(cuid)
        if not cu:
            continue
        for p in cu.prs:
            url_to_unit[p.url] = cuid
        urls = ", ".join(p.url for p in cu.prs)
        titles = "; ".join(p.title for p in cu.prs)
        cand_block_lines.append(f"- `{cuid}` — {titles} ({urls})")
    cand_block = "\n".join(cand_block_lines) or "_(none)_"

    primary = unit.prs[0] if unit.prs else None
    placeholders = {
        "source_pr_url": primary.url if primary else "",
        "source_pr_title": primary.title if primary else "",
        "unit_id": unit.unit_id,
        "conflict_files": "\n".join(f"- {f}" for f in conflict_files),
        "candidate_deps_block": cand_block,
        "base_branch": base_branch,
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    rendered = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)

    res = synthesize_text(
        config, rendered,
        label=f"discover-deps:{unit.unit_id}",
        timeout_seconds=config.ai_resolve.timeout_seconds,
        command=config.ai_resolve.command,
    )
    if not res.success or not res.text:
        warnings_acc.append(
            f"Claude refinement failed for {unit.unit_id!r}: "
            f"{res.error or 'no output'}; falling back to deterministic candidates"
        )
        return None

    # Distinguish "Claude said no prereqs" (a deliberate empty list under a
    # ``MISSING_PREREQS:`` line) from "Claude misformatted" (no marker line
    # at all). Without this check, both look identical to the parser and a
    # malformed response would silently look like a confident "no prereqs".
    if _MISSING_PREREQS_RE.search(res.text) is None:
        warnings_acc.append(
            f"Claude refinement for {unit.unit_id!r}: response did not "
            "include a MISSING_PREREQS: line; treating as malformed and "
            "falling back to deterministic candidates"
        )
        return None

    confirmed_urls, _reason = _parse_missing_prereqs(res.text)
    confirmed_units: list[str] = []
    seen: set[str] = set()
    for url in confirmed_urls:
        uid = url_to_unit.get(url)
        if uid is None:
            warnings_acc.append(
                f"Claude returned URL {url!r} for {unit.unit_id!r} that is "
                "not in the candidate-deps list; ignoring"
            )
            continue
        if uid in seen:
            continue
        seen.add(uid)
        confirmed_units.append(uid)
    return confirmed_units


# ---------------------------------------------------------------------------
# Components and articulation
# ---------------------------------------------------------------------------


def _components(
    nodes: dict[str, DAGNode],
    edges: set[tuple[str, str]],
    by_unit_id: dict[str, _CandidateUnit],
) -> tuple[list[DAGComponent], list[str]]:
    """Return (components, singletons). Components are the WCCs with ≥ 2
    nodes OR with at least one edge; singletons are leaf nodes with no
    edges in either direction.
    """
    # Build undirected adjacency
    adj: dict[str, set[str]] = {nid: set() for nid in nodes}
    for a, b in edges:
        if a in adj and b in adj:
            adj[a].add(b)
            adj[b].add(a)

    visited: set[str] = set()
    out_components: list[DAGComponent] = []
    out_singletons: list[str] = []
    next_id = 1

    sorted_ids = sorted(nodes.keys(), key=lambda nid: _node_sort_key(nodes[nid]))
    for nid in sorted_ids:
        if nid in visited:
            continue
        # BFS the WCC
        comp_nodes: list[str] = []
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp_nodes.append(cur)
            for nb in adj[cur]:
                if nb not in visited:
                    stack.append(nb)

        if len(comp_nodes) == 1 and not adj[comp_nodes[0]]:
            out_singletons.append(comp_nodes[0])
            continue

        topo = _topo_sort_within(comp_nodes, edges, by_unit_id)
        articulations = _articulation_points(comp_nodes, adj)
        comp_edges = sorted(
            [(a, b) for (a, b) in edges if a in comp_nodes and b in comp_nodes],
        )
        out_components.append(DAGComponent(
            component_id=f"wcc-{next_id}",
            unit_ids=topo,
            recommend_first=sorted(articulations),
            edges=comp_edges,
        ))
        next_id += 1

    out_singletons.sort()
    return out_components, out_singletons


def _topo_sort_within(
    comp_nodes: list[str],
    edges: set[tuple[str, str]],
    by_unit_id: dict[str, _CandidateUnit],
) -> list[str]:
    """Topo-sort a single component so deps come before dependents.

    Edge ``(a, b)`` means ``a`` depends on ``b``, so ``b`` should come first.
    """
    in_set = set(comp_nodes)
    indeg: dict[str, int] = {n: 0 for n in comp_nodes}
    succ: dict[str, list[str]] = {n: [] for n in comp_nodes}
    for a, b in edges:
        if a in in_set and b in in_set:
            indeg[a] += 1
            succ[b].append(a)
    ready = sorted(
        [n for n, d in indeg.items() if d == 0],
        key=lambda n: _candidate_sort_key(by_unit_id, n),
    )
    out: list[str] = []
    while ready:
        n = ready.pop(0)
        out.append(n)
        for s in succ[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                key = _candidate_sort_key(by_unit_id, s)
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if _candidate_sort_key(by_unit_id, ready[mid]) < key:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, s)
    if len(out) != len(comp_nodes):
        # Cycle — shouldn't happen post-cycle-break, but be defensive.
        leftover = [n for n in comp_nodes if n not in out]
        out.extend(sorted(leftover))
    return out


def _candidate_sort_key(
    by_unit_id: dict[str, _CandidateUnit], unit_id: str,
) -> tuple[str, int]:
    cu = by_unit_id.get(unit_id)
    if cu is None:
        return ("9999", 0)
    return (cu.earliest_merged_at or "9999", cu.prs[0].number if cu.prs else 0)


def _articulation_points(
    comp_nodes: list[str], adj: dict[str, set[str]],
) -> set[str]:
    """Tarjan's articulation-point algorithm on the undirected subgraph.

    Iterative implementation — Python's default recursion limit (~1000)
    isn't enough for a long-chain component (200+ PRs in series), and
    raising ``setrecursionlimit`` is fragile. The state machine below is
    the standard "neighbour iterator on the stack" formulation.
    """
    if not comp_nodes:
        return set()
    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    children_count: dict[str, int] = {}
    art: set[str] = set()
    timer = 0

    for root in comp_nodes:
        if root in disc:
            continue
        parent[root] = None
        children_count[root] = 0
        disc[root] = low[root] = timer
        timer += 1
        # Stack entries: (node, iterator over its neighbours).
        stack: list[tuple[str, "iter"]] = [(root, iter(sorted(adj[root])))]
        while stack:
            u, it = stack[-1]
            v = next(it, None)
            if v is None:
                # Done visiting u — propagate low-link to parent on pop.
                stack.pop()
                p = parent.get(u)
                if p is not None:
                    low[p] = min(low[p], low[u])
                    if low[u] >= disc[p] and parent.get(p) is not None:
                        art.add(p)
                continue
            if v not in disc:
                parent[v] = u
                children_count[v] = 0
                children_count[u] = children_count.get(u, 0) + 1
                disc[v] = low[v] = timer
                timer += 1
                stack.append((v, iter(sorted(adj[v]))))
            elif v != parent.get(u):
                low[u] = min(low[u], disc[v])
        # Root-of-DFS is articulation iff it has > 1 DFS child.
        if children_count.get(root, 0) > 1:
            art.add(root)
    return art


def _break_cycles(
    edges: set[tuple[str, str]],
    by_unit_id: dict[str, _CandidateUnit],
    warnings_acc: list[str],
) -> set[tuple[str, str]]:
    """Drop reverse edges in any 2-cycle to keep the graph acyclic.

    Spec calls for ``older→newer`` to be kept and ``newer→older`` dropped,
    keyed on ``(merged_at, number)``. For longer cycles we don't try to
    do anything clever — just warn.
    """
    out = set(edges)
    # Iterate in deterministic order so the warning ordering is stable
    # across runs and a re-run produces a byte-identical YAML report.
    seen_pairs: set[tuple[str, str]] = set()
    for (a, b) in sorted(edges):
        pair = (a, b) if a < b else (b, a)
        if pair in seen_pairs:
            continue
        if (b, a) in out and a != b:
            seen_pairs.add(pair)
            ka = _candidate_sort_key(by_unit_id, a)
            kb = _candidate_sort_key(by_unit_id, b)
            # Convention: an edge ``(x, y)`` means "x depends on y", so
            # we want to keep the edge that points from the NEWER unit
            # to the OLDER one (newer-depends-on-older).
            if ka == kb:
                # Same merged_at + same PR number across two distinct
                # units (degenerate but possible if a candidate has
                # ``merged_at=None``). Tie-break on unit_id lexically so
                # we drop a deterministic edge instead of leaving both
                # in place (which would produce a true 2-cycle and
                # break the topological sort downstream).
                if a < b:
                    out.discard((a, b))
                    warnings_acc.append(
                        f"cycle broken between {a!r} and {b!r}; kept "
                        f"{b!r} → {a!r} (lexical tie-break: identical merge_at)"
                    )
                else:
                    out.discard((b, a))
                    warnings_acc.append(
                        f"cycle broken between {a!r} and {b!r}; kept "
                        f"{a!r} → {b!r} (lexical tie-break: identical merge_at)"
                    )
            elif ka < kb:
                # a is older; b is newer. Keep (b, a) — newer depends on older.
                out.discard((a, b))
                warnings_acc.append(
                    f"cycle broken between {a!r} and {b!r}; kept {b!r} → {a!r} "
                    "(newer depends on older)"
                )
            else:
                out.discard((b, a))
                warnings_acc.append(
                    f"cycle broken between {a!r} and {b!r}; kept {a!r} → {b!r} "
                    "(newer depends on older)"
                )
    return out


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _make_node(
    cu: _CandidateUnit, *, deps: list[str], method: str,
    conflict_files: list[str],
) -> DAGNode:
    return DAGNode(
        unit_id=cu.unit_id,
        is_user_group=cu.is_user_group,
        pr_urls=[p.url for p in cu.prs],
        pr_titles=[p.title for p in cu.prs],
        earliest_merged_at=cu.earliest_merged_at,
        deps=sorted(deps),
        discovery_method=method,
        conflict_files_at_discovery=list(conflict_files),
    )


def _node_sort_key(node: DAGNode) -> tuple[str, str]:
    return (node.earliest_merged_at or "9999", node.unit_id)


def _default_report_path(config: Config, base_branch: str) -> Path:
    """``<config-dir>/discover-deps.<base-branch>.yaml``."""
    return config.config_path.parent / f"discover-deps.{base_branch}.yaml"


def _write_report(report: DiscoveryReport, path: Path) -> None:
    data: dict = {
        "base_branch": report.base_branch,
        "target_sha": report.target_sha,
        "generated_at": report.generated_at,
        "candidate_unit_count": report.candidate_unit_count,
    }
    if report.skipped_already_in_target:
        data["skipped_already_in_target"] = list(report.skipped_already_in_target)
    if report.warnings:
        data["warnings"] = list(report.warnings)
    if report.components:
        data["components"] = [
            {
                "component_id": c.component_id,
                "unit_ids": list(c.unit_ids),
                "recommend_first": list(c.recommend_first),
                "edges": [list(e) for e in c.edges],
            }
            for c in report.components
        ]
    if report.singletons:
        data["singletons"] = list(report.singletons)
    data["nodes"] = [
        {
            k: v
            for k, v in {
                "unit_id": n.unit_id,
                "is_user_group": n.is_user_group,
                "pr_urls": n.pr_urls,
                "pr_titles": n.pr_titles,
                "earliest_merged_at": n.earliest_merged_at,
                "deps": n.deps or None,
                "discovery_method": n.discovery_method,
                "conflict_files_at_discovery": (
                    n.conflict_files_at_discovery or None
                ),
            }.items()
            if v is not None
        }
        for n in report.nodes
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _write_session_overlay(
    report: DiscoveryReport, overlay_path: Path,
) -> None:
    """Emit the auto-deps sidecar overlay.

    Only writes entries for units that participate in the DAG (≥ 1 edge
    in or out). Pure singletons are omitted — the loader doesn't need
    them, they'll be re-discovered by ``by_labels`` / ``include_prs``.
    """
    relevant_unit_ids: set[str] = set()
    for c in report.components:
        relevant_unit_ids.update(c.unit_ids)
    nodes_by_id = {n.unit_id: n for n in report.nodes}

    overlay_groups: list[dict] = []
    for uid in sorted(
        relevant_unit_ids,
        key=lambda u: _node_sort_key(nodes_by_id[u]),
    ):
        node = nodes_by_id[uid]
        if node.is_user_group:
            # Don't replicate user groups in the overlay — they live in
            # the main session. We only emit deps as a separate single-PR
            # group when we own the entry (auto_discovered unit).
            continue
        entry: dict = {
            "id": uid,
            "prs": list(node.pr_urls),
            "auto_discovered": True,
        }
        if node.deps:
            entry["depends_on"] = list(node.deps)
        overlay_groups.append(entry)

    data: dict = {
        "generated_at": report.generated_at,
        "base_branch": report.base_branch,
    }
    if overlay_groups:
        data["groups"] = overlay_groups

    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    with open(overlay_path, "w") as f:
        f.write(
            "# AUTO-GENERATED by `releasy discover-deps --write-session`.\n"
            "# Hand-edits will be overwritten on next run; remove this file\n"
            "# (or move entries into the main session file) to make them permanent.\n\n"
        )
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _resolve_sha(repo_path: Path, ref: str) -> str:
    res = run_git(["rev-parse", ref], repo_path, check=False)
    if res.returncode != 0:
        return ""
    return res.stdout.strip()

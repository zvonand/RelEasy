"""STATUS.md generation from pipeline state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from releasy.config import Config
from releasy.github_ops import (
    get_origin_repo_slug,
    parse_pr_url,
    pr_ref_label,
)
from releasy.state import STATUS_DISPLAY_ORDER, FeatureState, PipelineState


STATUS_ICONS: dict[str, str] = {
    "needs_review": "\U0001f535 needs-review",
    "branch_created": "\U0001f7e1 branch-created",
    "conflict": "\U0001f534 conflict",
    "skipped": "\u23ed skipped",
}

STATUS_HEADINGS: dict[str, str] = {
    "needs_review": "Needs Review",
    "branch_created": "Branch Created \u2014 PR not opened yet",
    "conflict": "Conflict \u2014 unresolved (manual fix required)",
    "skipped": "Skipped",
}


def _format_date(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def generate_status_md(config: Config, state: PipelineState) -> str:
    """Generate the STATUS.md content.

    Renders one section per status (in :data:`STATUS_DISPLAY_ORDER`) with
    its own sub-table. Empty status groups are omitted. Statuses not in
    the canonical order get appended at the end alphabetically — defensive
    against future status additions.
    """
    run_date = _format_date(state.started_at) or "N/A"
    onto = state.onto or "N/A"

    origin_slug = get_origin_repo_slug(config)

    def _ai_cell(fs: FeatureState | None) -> str:
        if not fs or not fs.ai_resolved:
            return ""
        iters = f" ({fs.ai_iterations}\u00d7)" if fs.ai_iterations else ""
        return f"\U0001f916 ai-resolved{iters}"

    def _source_pr_cell(fs: FeatureState) -> str:
        if not fs.pr_url:
            return ""
        parsed = parse_pr_url(fs.pr_url)
        if parsed:
            owner, repo, n = parsed
            label = pr_ref_label(f"{owner}/{repo}", n, origin_slug)
        elif fs.pr_number:
            label = f"#{fs.pr_number}"
        else:
            label = "PR"
        return f"[{label}]({fs.pr_url})"

    def _rebase_pr_cell(fs: FeatureState) -> str:
        if not fs.rebase_pr_url:
            return ""
        n = fs.rebase_pr_url.rstrip("/").rsplit("/", 1)[-1]
        return f"[#{n}]({fs.rebase_pr_url})"

    def _feature_row(label: str, fs: FeatureState) -> str:
        base = f"`{fs.base_commit[:12]}`" if fs.base_commit else ""
        source_pr = _source_pr_cell(fs)
        rebase_pr = _rebase_pr_cell(fs)
        conflicts = (
            ", ".join(f"`{f}`" for f in fs.conflict_files)
            if fs.conflict_files else ""
        )
        return (
            f"| {label} | {_ai_cell(fs)} | {base} | {source_pr} "
            f"| {rebase_pr} | {conflicts} |"
        )

    # Group features by status. Only features with state entries are
    # rendered; static config.features that haven't run yet are
    # intentionally skipped.
    by_status: dict[str, list[tuple[str, FeatureState]]] = {}
    for fid, fs in state.features.items():
        by_status.setdefault(fs.status, []).append((fid, fs))

    lines: list[str] = [
        "## RelEasy Port Status",
        "",
        f"Last run: {run_date} \u00b7 Onto: `{onto}`",
        "",
    ]

    if not state.features:
        lines.append("_No ports tracked yet._")
        lines.append("")
        return "\n".join(lines)

    counts = ", ".join(
        f"{len(by_status[s])} {STATUS_ICONS.get(s, s)}"
        for s in STATUS_DISPLAY_ORDER if by_status.get(s)
    )
    if counts:
        lines.append(f"**Summary:** {counts}")
        lines.append("")

    ordered_statuses: list[str] = [
        s for s in STATUS_DISPLAY_ORDER if s in by_status
    ]
    extras = sorted(s for s in by_status if s not in STATUS_DISPLAY_ORDER)
    ordered_statuses.extend(extras)

    for status in ordered_statuses:
        rows = by_status[status]
        heading = STATUS_HEADINGS.get(status, status)
        icon = STATUS_ICONS.get(status, status)
        lines.append(f"### {icon} \u2014 {heading} ({len(rows)})")
        lines.append("")
        lines.append("| Branch | AI | Based On | Source PR | Rebase PR | Conflict Files |")
        lines.append("|--------|----|----------|-----------|-----------|----------------|")
        for fid, fs in rows:
            label = fs.branch_name or fid
            lines.append(_feature_row(label, fs))
        lines.append("")

    return "\n".join(lines)


def write_status_md(config: Config, state: PipelineState, path: Path | None = None) -> None:
    """Write STATUS.md to disk (next to config.yaml by default)."""
    if path is None:
        path = config.repo_dir / "STATUS.md"

    content = generate_status_md(config, state)
    with open(path, "w") as f:
        f.write(content)

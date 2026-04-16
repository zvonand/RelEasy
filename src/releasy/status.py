"""STATUS.md generation from pipeline state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from releasy.config import Config
from releasy.state import FeatureState, PipelineState


STATUS_ICONS: dict[str, str] = {
    "ok": "\u2705 ok",
    "conflict": "\U0001f534 conflict",
    "resolved": "\U0001f535 resolved",
    "skipped": "\u23ed skipped",
    "disabled": "\u23f8 disabled",
    "pending": "\u23f3 pending",
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
    """Generate the STATUS.md content."""
    run_date = _format_date(state.started_at) or "N/A"
    onto = state.onto or "N/A"

    lines = [
        "## RelEasy Port Status",
        "",
        f"Last run: {run_date} \u00b7 Upstream commit: `{onto}`",
        "",
        "| Branch | Status | AI | Based On | Source PR | Conflict Files |",
        "|--------|--------|----|----------|-----------|----------------|",
    ]

    def _ai_cell(fs: FeatureState | None) -> str:
        if not fs or not fs.ai_resolved:
            return ""
        iters = f" ({fs.ai_iterations}\u00d7)" if fs.ai_iterations else ""
        return f"\U0001f916 ai-resolved{iters}"

    def _feature_row(label: str, fs: FeatureState | None, status_str: str) -> str:
        if fs is None:
            return f"| {label} | {status_str} | | | | |"
        base = f"`{fs.base_commit[:12]}`" if fs.base_commit else ""
        source_pr = ""
        if fs.pr_url:
            pr_label = f"#{fs.pr_number}" if fs.pr_number else "PR"
            source_pr = f"[{pr_label}]({fs.pr_url})"
        conflicts = ", ".join(f"`{f}`" for f in fs.conflict_files) if fs.conflict_files else ""
        return (
            f"| {label} | {status_str} | {_ai_cell(fs)} | {base} | {source_pr} | {conflicts} |"
        )

    shown_ids: set[str] = set()
    for feat in config.features:
        fs = state.features.get(feat.id)
        shown_ids.add(feat.id)
        if fs is None:
            status_str = STATUS_ICONS["disabled"] if not feat.enabled else STATUS_ICONS["pending"]
            lines.append(_feature_row(feat.source_branch or feat.id, None, status_str))
            continue

        label = fs.branch_name or feat.source_branch or feat.id
        status_str = STATUS_ICONS.get(fs.status, fs.status)
        lines.append(_feature_row(label, fs, status_str))

    for fid, fs in state.features.items():
        if fid in shown_ids:
            continue
        label = fs.branch_name or fid
        status_str = STATUS_ICONS.get(fs.status, fs.status)
        lines.append(_feature_row(label, fs, status_str))

    lines.append("")
    return "\n".join(lines)


def write_status_md(config: Config, state: PipelineState, path: Path | None = None) -> None:
    """Write STATUS.md to disk (next to config.yaml by default)."""
    if path is None:
        path = config.repo_dir / "STATUS.md"

    content = generate_status_md(config, state)
    with open(path, "w") as f:
        f.write(content)

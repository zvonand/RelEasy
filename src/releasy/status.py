"""STATUS.md generation from pipeline state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from releasy.config import Config
from releasy.state import PipelineState


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
        "## RelEasy Branch Status",
        "",
        f"Last run: {run_date} \u00b7 Upstream commit: `{onto}`",
        "",
        "| Branch | Status | Based On | Conflict Files |",
        "|--------|--------|----------|----------------|",
    ]

    # CI branch row
    ci = state.ci_branch
    ci_label = ci.branch_name or config.ci.branch_prefix
    ci_status = STATUS_ICONS.get(ci.status, ci.status)
    ci_base = f"`{ci.base_commit[:12]}`" if ci.base_commit else ""
    ci_conflicts = ", ".join(f"`{f}`" for f in ci.conflict_files) if ci.conflict_files else ""
    lines.append(f"| {ci_label} | {ci_status} | {ci_base} | {ci_conflicts} |")

    # Feature branch rows (source-branch and PR-based)
    shown_ids: set[str] = set()
    for feat in config.features:
        fs = state.features.get(feat.id)
        shown_ids.add(feat.id)
        if fs is None:
            status_str = STATUS_ICONS["disabled"] if not feat.enabled else STATUS_ICONS["pending"]
            lines.append(f"| {feat.source_branch} | {status_str} | | |")
            continue

        label = fs.branch_name or feat.source_branch
        if fs.pr_url:
            label = f"[{label}]({fs.pr_url})"
        status_str = STATUS_ICONS.get(fs.status, fs.status)
        base = f"`{fs.base_commit[:12]}`" if fs.base_commit else ""
        conflicts = ", ".join(f"`{f}`" for f in fs.conflict_files) if fs.conflict_files else ""
        lines.append(f"| {label} | {status_str} | {base} | {conflicts} |")

    # PR features in state but not in config (from a previous run)
    for fid, fs in state.features.items():
        if fid in shown_ids:
            continue
        label = fs.branch_name or fid
        if fs.pr_url:
            label = f"[{label}]({fs.pr_url})"
        status_str = STATUS_ICONS.get(fs.status, fs.status)
        base = f"`{fs.base_commit[:12]}`" if fs.base_commit else ""
        conflicts = ", ".join(f"`{f}`" for f in fs.conflict_files) if fs.conflict_files else ""
        lines.append(f"| {label} | {status_str} | {base} | {conflicts} |")

    lines.append("")
    return "\n".join(lines)


def write_status_md(config: Config, state: PipelineState, path: Path | None = None) -> None:
    """Write STATUS.md to disk."""
    if path is None:
        path = Path.cwd() / "STATUS.md"

    content = generate_status_md(config, state)
    with open(path, "w") as f:
        f.write(content)

"""Pipeline state management — read/write state.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

BranchStatus = Literal[
    "needs_review",
    "branch_created",
    "conflict",
    "skipped",
]
PipelinePhase = Literal["init", "ports_done"]


# Order in which status groups are shown to humans (STATUS.md sections,
# `releasy status` sub-tables). Highest-attention first.
STATUS_DISPLAY_ORDER: tuple[str, ...] = (
    "conflict",
    "branch_created",
    "needs_review",
    "skipped",
)


# Legacy values older state files (or older code paths) may carry.
#   ok / resolved / pending / disabled  → ``needs_review``
#                                         (catch-all "port done, human
#                                         needs to review the PR" state;
#                                         ``ai_resolved`` carries the
#                                         AI-was-involved signal).
#   needs_resolution                    → ``conflict``
#                                         (used to be a separate "AI
#                                         gave up" state; it's just an
#                                         unresolved conflict — the
#                                         ``failed_step_index`` /
#                                         ``partial_pr_count`` fields
#                                         still describe how it failed).
_LEGACY_STATUS_ALIASES = {
    "ok": "needs_review",
    "resolved": "needs_review",
    "pending": "needs_review",
    "disabled": "needs_review",
    "needs_resolution": "conflict",
}


@dataclass
class FeatureState:
    status: BranchStatus = "needs_review"
    branch_name: str | None = None
    base_commit: str | None = None
    conflict_files: list[str] = field(default_factory=list)
    # Source PR meta. For singleton features, the *_url / *_number / *_title
    # fields hold the one and only PR. For sequential PR groups, they hold
    # the FIRST PR (for backward-compat with display code), and the
    # ``pr_numbers`` / ``pr_urls`` lists hold every PR in cherry-pick order.
    pr_url: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    pr_numbers: list[int] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # GitHub login of the (first) source PR's author. Used by the project
    # board sync to seed the ``Assignee Dev`` field once, when the card is
    # first created. Stored on state so re-runs and ``releasy continue``
    # can rebuild the board without re-fetching every PR from GitHub.
    pr_author: str | None = None
    rebase_pr_url: str | None = None  # auto-created PR targeting base branch
    ai_resolved: bool = False
    ai_iterations: int | None = None
    # Cumulative USD cost reported by Claude across every resolve
    # invocation that touched this entry (cherry-pick steps + later
    # ``releasy refresh`` merges). ``None`` means we have no cost data
    # for this entry — either AI never ran, or Claude didn't report a
    # cost. Synced to the GitHub Project board's "AI Cost" number field.
    ai_cost_usd: float | None = None
    # For partially-applied groups: 0-based index of the cherry-pick step that
    # failed conflict resolution, and how many earlier picks were committed.
    failed_step_index: int | None = None
    partial_pr_count: int | None = None


@dataclass
class PipelineState:
    started_at: str | None = None
    onto: str | None = None
    phase: PipelinePhase = "init"
    base_branch: str | None = None
    features: dict[str, FeatureState] = field(default_factory=dict)

    def set_started(self, onto: str) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.onto = onto

    def all_features_ok(self) -> bool:
        return all(
            fs.status == "needs_review"
            for fs in self.features.values()
        )


def load_state(repo_dir: Path | None = None) -> PipelineState:
    """Load pipeline state from state.yaml, returning empty state if file doesn't exist."""
    if repo_dir is None:
        repo_dir = Path.cwd()
    state_path = repo_dir / "state.yaml"

    if not state_path.exists():
        return PipelineState()

    with open(state_path) as f:
        raw = yaml.safe_load(f)

    if not raw or "last_run" not in raw:
        return PipelineState()

    run = raw["last_run"]

    features: dict[str, FeatureState] = {}
    for fid, fraw in run.get("features", {}).items():
        raw_status = fraw.get("status", "needs_review")
        status = _LEGACY_STATUS_ALIASES.get(raw_status, raw_status)
        features[fid] = FeatureState(
            status=status,
            branch_name=fraw.get("branch_name"),
            base_commit=fraw.get("base_commit"),
            conflict_files=fraw.get("conflict_files", []),
            pr_url=fraw.get("pr_url"),
            pr_number=fraw.get("pr_number"),
            pr_title=fraw.get("pr_title"),
            pr_body=fraw.get("pr_body"),
            pr_numbers=fraw.get("pr_numbers", []) or [],
            pr_urls=fraw.get("pr_urls", []) or [],
            pr_author=fraw.get("pr_author"),
            rebase_pr_url=fraw.get("rebase_pr_url"),
            ai_resolved=fraw.get("ai_resolved", False),
            ai_iterations=fraw.get("ai_iterations"),
            ai_cost_usd=fraw.get("ai_cost_usd"),
            failed_step_index=fraw.get("failed_step_index"),
            partial_pr_count=fraw.get("partial_pr_count"),
        )

    phase = run.get("phase", "init")
    if phase not in ("init", "ports_done"):
        phase = "init"

    return PipelineState(
        started_at=run.get("started_at"),
        onto=run.get("onto"),
        phase=phase,
        base_branch=run.get("base_branch"),
        features=features,
    )


def save_state(state: PipelineState, repo_dir: Path | None = None) -> None:
    """Persist pipeline state to state.yaml."""
    if repo_dir is None:
        repo_dir = Path.cwd()
    state_path = repo_dir / "state.yaml"

    features_data = {}
    for fid, fs in state.features.items():
        entry: dict = {"status": fs.status}
        if fs.branch_name:
            entry["branch_name"] = fs.branch_name
        if fs.base_commit:
            entry["base_commit"] = fs.base_commit
        if fs.conflict_files:
            entry["conflict_files"] = fs.conflict_files
        if fs.pr_url:
            entry["pr_url"] = fs.pr_url
        if fs.pr_number:
            entry["pr_number"] = fs.pr_number
        if fs.pr_title:
            entry["pr_title"] = fs.pr_title
        if fs.pr_body:
            entry["pr_body"] = fs.pr_body
        if fs.pr_numbers and len(fs.pr_numbers) > 1:
            entry["pr_numbers"] = fs.pr_numbers
        if fs.pr_urls and len(fs.pr_urls) > 1:
            entry["pr_urls"] = fs.pr_urls
        if fs.pr_author:
            entry["pr_author"] = fs.pr_author
        if fs.rebase_pr_url:
            entry["rebase_pr_url"] = fs.rebase_pr_url
        if fs.ai_resolved:
            entry["ai_resolved"] = True
        if fs.ai_iterations is not None:
            entry["ai_iterations"] = fs.ai_iterations
        if fs.ai_cost_usd is not None:
            entry["ai_cost_usd"] = float(fs.ai_cost_usd)
        if fs.failed_step_index is not None:
            entry["failed_step_index"] = fs.failed_step_index
        if fs.partial_pr_count is not None:
            entry["partial_pr_count"] = fs.partial_pr_count
        features_data[fid] = entry

    data = {
        "last_run": {
            "started_at": state.started_at,
            "onto": state.onto,
            "phase": state.phase,
            "base_branch": state.base_branch,
            "features": features_data,
        }
    }

    with open(state_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

"""Pipeline state management — read/write state.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

BranchStatus = Literal["pending", "ok", "conflict", "resolved", "skipped", "disabled"]


@dataclass
class CIBranchState:
    status: BranchStatus = "pending"
    branch_name: str | None = None  # versioned branch name, e.g. ci/antalya/abc12345
    base_commit: str | None = None  # upstream commit the branch was created from
    conflict_files: list[str] = field(default_factory=list)


@dataclass
class FeatureState:
    status: BranchStatus = "pending"
    branch_name: str | None = None  # versioned branch name, e.g. feature/s3-disk/abc12345
    base_commit: str | None = None  # upstream commit (via CI branch) the branch was created from
    conflict_files: list[str] = field(default_factory=list)
    pr_url: str | None = None  # GitHub PR URL (for PR-sourced features)
    pr_number: int | None = None  # PR number (for PR-sourced features)
    pr_title: str | None = None  # original PR title (preserved for release PR creation)
    pr_body: str | None = None  # original PR body (preserved for release PR creation)


@dataclass
class PipelineState:
    started_at: str | None = None
    onto: str | None = None
    ci_branch: CIBranchState = field(default_factory=CIBranchState)
    features: dict[str, FeatureState] = field(default_factory=dict)

    def set_started(self, onto: str) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.onto = onto

    def all_features_ok(self) -> bool:
        return all(
            fs.status in ("ok", "disabled")
            for fs in self.features.values()
        )


def load_state(state_path: Path | None = None) -> PipelineState:
    """Load pipeline state from state.yaml, returning empty state if file doesn't exist."""
    if state_path is None:
        state_path = Path.cwd() / "state.yaml"

    if not state_path.exists():
        return PipelineState()

    with open(state_path) as f:
        raw = yaml.safe_load(f)

    if not raw or "last_run" not in raw:
        return PipelineState()

    run = raw["last_run"]
    ci_raw = run.get("ci_branch", {})
    ci_state = CIBranchState(
        status=ci_raw.get("status", "pending"),
        branch_name=ci_raw.get("branch_name"),
        base_commit=ci_raw.get("base_commit"),
        conflict_files=ci_raw.get("conflict_files", []),
    )

    features: dict[str, FeatureState] = {}
    for fid, fraw in run.get("features", {}).items():
        features[fid] = FeatureState(
            status=fraw.get("status", "pending"),
            branch_name=fraw.get("branch_name"),
            base_commit=fraw.get("base_commit"),
            conflict_files=fraw.get("conflict_files", []),
            pr_url=fraw.get("pr_url"),
            pr_number=fraw.get("pr_number"),
            pr_title=fraw.get("pr_title"),
            pr_body=fraw.get("pr_body"),
        )

    return PipelineState(
        started_at=run.get("started_at"),
        onto=run.get("onto"),
        ci_branch=ci_state,
        features=features,
    )


def save_state(state: PipelineState, state_path: Path | None = None) -> None:
    """Persist pipeline state to state.yaml."""
    if state_path is None:
        state_path = Path.cwd() / "state.yaml"

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
        features_data[fid] = entry

    ci_data: dict = {"status": state.ci_branch.status}
    if state.ci_branch.branch_name:
        ci_data["branch_name"] = state.ci_branch.branch_name
    if state.ci_branch.base_commit:
        ci_data["base_commit"] = state.ci_branch.base_commit
    if state.ci_branch.conflict_files:
        ci_data["conflict_files"] = state.ci_branch.conflict_files

    data = {
        "last_run": {
            "started_at": state.started_at,
            "onto": state.onto,
            "ci_branch": ci_data,
            "features": features_data,
        }
    }

    with open(state_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

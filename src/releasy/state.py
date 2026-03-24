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
    rebased_onto: str | None = None
    conflict_files: list[str] = field(default_factory=list)


@dataclass
class FeatureState:
    status: BranchStatus = "pending"
    rebased_onto: str | None = None
    conflict_files: list[str] = field(default_factory=list)


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
        rebased_onto=ci_raw.get("rebased_onto"),
        conflict_files=ci_raw.get("conflict_files", []),
    )

    features: dict[str, FeatureState] = {}
    for fid, fraw in run.get("features", {}).items():
        features[fid] = FeatureState(
            status=fraw.get("status", "pending"),
            rebased_onto=fraw.get("rebased_onto"),
            conflict_files=fraw.get("conflict_files", []),
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
        if fs.rebased_onto:
            entry["rebased_onto"] = fs.rebased_onto
        if fs.conflict_files:
            entry["conflict_files"] = fs.conflict_files
        features_data[fid] = entry

    ci_data: dict = {"status": state.ci_branch.status}
    if state.ci_branch.rebased_onto:
        ci_data["rebased_onto"] = state.ci_branch.rebased_onto
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

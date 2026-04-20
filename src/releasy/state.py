"""Pipeline state management — read/write per-project state files.

State no longer lives in the user's repo dir. Each project (identified
by ``Config.name``) gets its own state file under
``state_root() / "<name>.state.yaml"`` (XDG state location by default,
overridable via ``$RELEASY_STATE_DIR``).

The state file additionally carries the absolute ``config_path`` of the
config that owns it, so we can:

  * surface a friendly listing in ``releasy list``,
  * detect "wait, this state belongs to a different config.yaml"
    collisions when somebody copies a config without changing ``name:``.

Use :func:`verify_ownership` before mutating; use :func:`adopt_ownership`
to forcibly rebind state to the current config (the ``releasy adopt``
command).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

from releasy.config import Config, state_file_path

BranchStatus = Literal[
    "needs_review",
    "branch_created",
    "conflict",
    "skipped",
]
PipelinePhase = Literal["init", "ports_done"]


# Order in which status groups are shown to humans (``releasy status``
# sub-tables, ``releasy list`` summary). Highest-attention first.
STATUS_DISPLAY_ORDER: tuple[str, ...] = (
    "conflict",
    "branch_created",
    "needs_review",
    "skipped",
)


# Most recent ``config_path`` history entries to keep in the state file.
# Trimmed to a small window — the field is mostly an audit trail for
# users who move a config repeatedly; nobody needs the full history.
_CONFIG_PATH_HISTORY_MAX = 8


class OwnershipCollisionError(Exception):
    """Raised when a state file is owned by a different config than the one loaded."""

    def __init__(
        self,
        name: str,
        state_path: Path,
        loaded_config: Path,
        stored_config: Path,
    ) -> None:
        self.name = name
        self.state_path = state_path
        self.loaded_config = loaded_config
        self.stored_config = stored_config
        super().__init__(
            f"Project name {name!r} is already tracked at "
            f"{stored_config}, but you ran releasy with config "
            f"{loaded_config}. Either pick a different 'name:' in the "
            f"new config, delete the old config, or run "
            f"`releasy adopt` to rebind state to the new config."
        )


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
    # Provenance (filled by load_state / save_state, not user-visible config):
    config_path: str | None = None
    config_path_history: list[str] = field(default_factory=list)

    def set_started(self, onto: str) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.onto = onto

    def all_features_ok(self) -> bool:
        return all(
            fs.status == "needs_review"
            for fs in self.features.values()
        )


def _parse_features(raw_features: dict) -> dict[str, FeatureState]:
    features: dict[str, FeatureState] = {}
    for fid, fraw in (raw_features or {}).items():
        features[fid] = FeatureState(
            status=fraw.get("status", "needs_review"),
            branch_name=fraw.get("branch_name"),
            base_commit=fraw.get("base_commit"),
            conflict_files=fraw.get("conflict_files", []) or [],
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
    return features


def _read_raw_state(path: Path) -> dict:
    """Read ``path`` as a state-file dict, returning ``{}`` if missing/empty."""
    if not path.exists():
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return {}
    return raw


def load_state(config: Config) -> PipelineState:
    """Load the pipeline state for ``config``'s project.

    Returns an empty :class:`PipelineState` (with provenance fields filled
    from the config) when the state file does not exist yet — matches the
    "first run" case so callers don't need to special-case it.
    """
    state_path = state_file_path(config.name)
    raw = _read_raw_state(state_path)

    run = raw.get("last_run") if isinstance(raw.get("last_run"), dict) else {}
    features = _parse_features(run.get("features", {}) or {})

    phase = run.get("phase", "init")
    if phase not in ("init", "ports_done"):
        phase = "init"

    return PipelineState(
        started_at=run.get("started_at"),
        onto=run.get("onto"),
        phase=phase,
        base_branch=run.get("base_branch"),
        features=features,
        config_path=raw.get("config_path"),
        config_path_history=list(raw.get("config_path_history", []) or []),
    )


def save_state(state: PipelineState, config: Config) -> None:
    """Persist ``state`` to ``config``'s per-project state file.

    Always rewrites ``config_path`` to the loaded config's absolute
    location and appends to ``config_path_history`` if it changed.
    """
    state_path = state_file_path(config.name)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    current_cfg = str(config.config_path.resolve())
    history = list(state.config_path_history or [])
    if state.config_path and state.config_path != current_cfg:
        if state.config_path not in history:
            history.append(state.config_path)
        history = history[-_CONFIG_PATH_HISTORY_MAX:]
    state.config_path = current_cfg
    state.config_path_history = history

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

    data: dict = {
        "name": config.name,
        "config_path": current_cfg,
    }
    if history:
        data["config_path_history"] = history
    data["last_run"] = {
        "started_at": state.started_at,
        "onto": state.onto,
        "phase": state.phase,
        "base_branch": state.base_branch,
        "features": features_data,
    }

    with open(state_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def verify_ownership(config: Config) -> None:
    """Raise :class:`OwnershipCollisionError` if state belongs to a different config.

    No-op when:

    * the state file does not yet exist (first-time run),
    * the file exists but carries no ``config_path`` (legacy or hand-edited),
    * the stored ``config_path`` matches the loaded config's path.
    """
    state_path = state_file_path(config.name)
    raw = _read_raw_state(state_path)
    stored = raw.get("config_path")
    if not stored:
        return
    loaded_resolved = config.config_path.resolve()
    try:
        stored_resolved = Path(stored).resolve()
    except (OSError, RuntimeError):
        # If the stored path can no longer be resolved (deleted, missing
        # mount, …) there's no meaningful collision to flag — treat the
        # current config as the new owner.
        return
    if stored_resolved == loaded_resolved:
        return
    raise OwnershipCollisionError(
        name=config.name,
        state_path=state_path,
        loaded_config=loaded_resolved,
        stored_config=stored_resolved,
    )


def adopt_ownership(config: Config) -> tuple[Path | None, Path]:
    """Forcibly rebind the state file's ``config_path`` to the current config.

    Returns ``(previous_config_path, new_config_path)``. ``previous`` is
    ``None`` when there was no state file yet (creates a fresh one) or
    when the file already pointed at the current config.
    """
    state_path = state_file_path(config.name)
    state = load_state(config)
    previous: Path | None = None
    if state.config_path:
        try:
            prev = Path(state.config_path).resolve()
        except (OSError, RuntimeError):
            prev = None
        if prev and prev != config.config_path.resolve():
            previous = prev
    save_state(state, config)
    return previous, state_path

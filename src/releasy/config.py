"""Configuration loading and validation from config.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def extract_version_suffix(onto: str) -> str:
    """Extract a version suffix from a tag or ref for branch naming.

    v26.3.4.234-lts → 26.3
    v26.2.5.45-stable → 26.2
    Raw SHA → first 8 chars
    """
    m = re.match(r"v?(\d+)\.(\d+)", onto)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    # Looks like a hex SHA
    if re.fullmatch(r"[0-9a-f]{7,40}", onto):
        return onto[:8]
    return onto


@dataclass
class UpstreamConfig:
    remote: str
    remote_name: str = "upstream"


@dataclass
class OriginConfig:
    remote: str
    remote_name: str = "origin"


@dataclass
class FeatureConfig:
    id: str
    description: str
    source_branch: str  # existing branch where feature commits live
    enabled: bool = True
    depends_on: list[str] = field(default_factory=list)


@dataclass
class PRSourceConfig:
    labels: list[str]
    description: str = ""
    merged_only: bool = False
    auto_pr: bool = False
    if_exists: str = "skip"  # "skip" or "redo"


@dataclass
class PRSourcesConfig:
    """PR discovery and filtering.

    Set arithmetic: union(by_labels) − exclude_labels + include_prs − exclude_prs
    """
    by_labels: list[PRSourceConfig] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_prs: list[str] = field(default_factory=list)
    exclude_prs: list[str] = field(default_factory=list)


@dataclass
class NotificationsConfig:
    github_project: str | None = None


def _default_allowed_tools() -> list[str]:
    return [
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(git:*)", "Bash(gh:*)", "Bash(cd:*)",
        "Bash(ninja:*)", "Bash(cmake:*)", "Bash(make:*)",
        "Bash(ls:*)", "Bash(cat:*)", "Bash(rg:*)",
    ]


@dataclass
class AIResolveConfig:
    """Claude-driven conflict resolver configuration."""
    enabled: bool = False
    command: str = "claude"
    prompt_file: str = "prompts/resolve_conflict.md"
    allowed_tools: list[str] = field(default_factory=_default_allowed_tools)
    max_iterations: int = 5
    timeout_seconds: int = 7200  # 2h
    build_command: str = "cd build && ninja"
    label: str = "ai-resolved"
    label_color: str = "8B5CF6"
    extra_args: list[str] = field(default_factory=list)


@dataclass
class Config:
    origin: OriginConfig
    project: str  # short project identifier, e.g. "antalya"
    upstream: UpstreamConfig | None = None
    features: list[FeatureConfig] = field(default_factory=list)
    pr_sources: PRSourcesConfig = field(default_factory=PRSourcesConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    ai_resolve: AIResolveConfig = field(default_factory=AIResolveConfig)
    config_path: Path = field(default_factory=lambda: Path.cwd() / "config.yaml")
    work_dir: Path | None = None
    push: bool = False

    @property
    def repo_dir(self) -> Path:
        """Directory containing config.yaml (and state.yaml, STATUS.md)."""
        return self.config_path.parent

    def resolve_work_dir(self, cli_override: Path | None = None) -> Path:
        """Resolve the working directory for git clone operations.

        Priority: CLI --work-dir > config work_dir > current directory.
        """
        if cli_override is not None:
            return cli_override.resolve()
        if self.work_dir is not None:
            return self.work_dir.resolve()
        return Path.cwd()

    @property
    def enabled_features(self) -> list[FeatureConfig]:
        return [f for f in self.features if f.enabled]

    def get_feature(self, feature_id: str) -> FeatureConfig | None:
        return next((f for f in self.features if f.id == feature_id), None)

    @property
    def project_name(self) -> str:
        return self.project

    def get_feature_by_branch(self, branch: str, onto: str = "") -> FeatureConfig | None:
        """Match by source_branch or by versioned branch prefix."""
        for f in self.features:
            if f.source_branch == branch:
                return f
            if onto:
                prefix = self.feature_branch_prefix(f.id, onto)
                if branch.startswith(prefix):
                    return f
        return None

    def base_branch_name(self, onto: str) -> str:
        """Build the base branch name: <project>-<version>.

        v26.3.4.234-lts → antalya-26.3
        <sha> → antalya-<sha8>
        """
        suffix = extract_version_suffix(onto)
        return f"{self.project_name}-{suffix}"

    def feature_branch_prefix(self, feature_id: str, onto: str) -> str:
        """Build the prefix for feature branches: feature/<base>/<id>"""
        return f"feature/{self.base_branch_name(onto)}/{feature_id}"

    def feature_branch_name(self, feature_id: str, onto: str) -> str:
        """Build a feature branch name: feature/<base>/<id>"""
        return f"feature/{self.base_branch_name(onto)}/{feature_id}"


def load_config(config_path: Path | None = None) -> Config:
    """Load and validate config from config.yaml."""
    if config_path is None:
        config_path = Path.cwd() / "config.yaml"

    config_path = config_path.resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not raw:
        raise ValueError("Config file is empty")

    origin = OriginConfig(
        remote=raw["origin"]["remote"],
        remote_name=raw["origin"].get("remote_name", "origin"),
    )

    upstream_raw = raw.get("upstream")
    upstream = None
    if upstream_raw:
        upstream = UpstreamConfig(
            remote=upstream_raw["remote"],
            remote_name=upstream_raw.get("remote_name", "upstream"),
        )

    project = raw.get("project")
    if not project:
        raise ValueError(
            "Config must set 'project' (e.g. 'antalya'). "
            "This is used to name the base and port branches."
        )

    features = []
    for feat_raw in raw.get("features", []):
        features.append(
            FeatureConfig(
                id=feat_raw["id"],
                description=feat_raw["description"],
                source_branch=feat_raw["source_branch"],
                enabled=feat_raw.get("enabled", True),
                depends_on=feat_raw.get("depends_on", []),
            )
        )

    ps_raw = raw.get("pr_sources", {})
    by_labels = []
    for entry in ps_raw.get("by_labels", []):
        raw_labels = entry.get("labels", [])
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        by_labels.append(
            PRSourceConfig(
                labels=raw_labels,
                description=entry.get("description", ""),
                merged_only=entry.get("merged_only", False),
                auto_pr=entry.get("auto_pr", False),
                if_exists=entry.get("if_exists", "skip"),
            )
        )
    pr_sources = PRSourcesConfig(
        by_labels=by_labels,
        exclude_labels=ps_raw.get("exclude_labels", []),
        include_prs=ps_raw.get("include_prs", []),
        exclude_prs=ps_raw.get("exclude_prs", []),
    )

    notifications = NotificationsConfig(
        github_project=raw.get("notifications", {}).get("github_project"),
    )

    ai_raw = raw.get("ai_resolve", {}) or {}
    ai_resolve = AIResolveConfig(
        enabled=ai_raw.get("enabled", False),
        command=ai_raw.get("command", "claude"),
        prompt_file=ai_raw.get("prompt_file", "prompts/resolve_conflict.md"),
        allowed_tools=ai_raw.get("allowed_tools") or _default_allowed_tools(),
        max_iterations=int(ai_raw.get("max_iterations", 5)),
        timeout_seconds=int(ai_raw.get("timeout_seconds", 7200)),
        build_command=ai_raw.get("build_command", "cd build && ninja"),
        label=ai_raw.get("label", "ai-resolved"),
        label_color=ai_raw.get("label_color", "8B5CF6"),
        extra_args=ai_raw.get("extra_args", []) or [],
    )

    raw_work_dir = raw.get("work_dir")
    work_dir = Path(raw_work_dir).resolve() if raw_work_dir else None

    return Config(
        origin=origin,
        project=project,
        upstream=upstream,
        features=features,
        pr_sources=pr_sources,
        notifications=notifications,
        ai_resolve=ai_resolve,
        config_path=config_path,
        work_dir=work_dir,
        push=raw.get("push", False),
    )


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Persist config back to config.yaml."""
    if config_path is None:
        config_path = config.config_path

    data: dict = {
        "origin": {
            "remote": config.origin.remote,
            "remote_name": config.origin.remote_name,
        },
        "project": config.project,
    }

    if config.upstream:
        data["upstream"] = {
            "remote": config.upstream.remote,
            "remote_name": config.upstream.remote_name,
        }

    data["features"] = [
        {
            k: v
            for k, v in {
                "id": f.id,
                "description": f.description,
                "source_branch": f.source_branch,
                "enabled": f.enabled,
                "depends_on": f.depends_on or None,
            }.items()
            if v is not None
        }
        for f in config.features
    ]

    if config.work_dir:
        data["work_dir"] = str(config.work_dir)

    ps = config.pr_sources
    if ps.by_labels or ps.exclude_labels or ps.include_prs or ps.exclude_prs:
        ps_data: dict = {}
        if ps.by_labels:
            ps_data["by_labels"] = [
                {
                    k: v
                    for k, v in {
                        "labels": entry.labels,
                        "description": entry.description or None,
                        "merged_only": entry.merged_only or None,
                        "auto_pr": entry.auto_pr or None,
                        "if_exists": entry.if_exists if entry.if_exists != "skip" else None,
                    }.items()
                    if v is not None
                }
                for entry in ps.by_labels
            ]
        if ps.exclude_labels:
            ps_data["exclude_labels"] = ps.exclude_labels
        if ps.include_prs:
            ps_data["include_prs"] = ps.include_prs
        if ps.exclude_prs:
            ps_data["exclude_prs"] = ps.exclude_prs
        data["pr_sources"] = ps_data

    if config.notifications.github_project:
        data["notifications"] = {"github_project": config.notifications.github_project}

    ai = config.ai_resolve
    ai_defaults = AIResolveConfig()
    ai_data: dict = {}
    if ai.enabled != ai_defaults.enabled:
        ai_data["enabled"] = ai.enabled
    if ai.command != ai_defaults.command:
        ai_data["command"] = ai.command
    if ai.prompt_file != ai_defaults.prompt_file:
        ai_data["prompt_file"] = ai.prompt_file
    if ai.allowed_tools != _default_allowed_tools():
        ai_data["allowed_tools"] = ai.allowed_tools
    if ai.max_iterations != ai_defaults.max_iterations:
        ai_data["max_iterations"] = ai.max_iterations
    if ai.timeout_seconds != ai_defaults.timeout_seconds:
        ai_data["timeout_seconds"] = ai.timeout_seconds
    if ai.build_command != ai_defaults.build_command:
        ai_data["build_command"] = ai.build_command
    if ai.label != ai_defaults.label:
        ai_data["label"] = ai.label
    if ai.label_color != ai_defaults.label_color:
        ai_data["label_color"] = ai.label_color
    if ai.extra_args:
        ai_data["extra_args"] = ai.extra_args
    if ai_data:
        data["ai_resolve"] = ai_data

    if config.push:
        data["push"] = True

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_github_token() -> str | None:
    return os.environ.get("RELEASY_GITHUB_TOKEN")


def get_ssh_key_path() -> str | None:
    return os.environ.get("RELEASY_SSH_KEY_PATH")


def get_repo_dir(config: Config | None = None) -> Path:
    """Get the repo root directory (where config.yaml lives).

    Prefer using config.repo_dir directly when a Config is available.
    """
    if config is not None:
        return config.repo_dir
    return Path.cwd()

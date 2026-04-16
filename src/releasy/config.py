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
class CIConfig:
    branch_prefix: str  # e.g. "ci/antalya" → branches become ci/antalya/<sha8>
    source_branch: str = ""  # CI source branch; empty = skip CI rebase
    if_exists: str = "skip"  # "skip" or "redo"


@dataclass
class FeatureConfig:
    id: str
    description: str
    source_branch: str  # existing branch where feature commits live
    enabled: bool = True
    depends_on: list[str] = field(default_factory=list)  # feature IDs this depends on (ordering TBD)


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


@dataclass
class Config:
    origin: OriginConfig
    ci: CIConfig
    upstream: UpstreamConfig | None = None
    features: list[FeatureConfig] = field(default_factory=list)
    pr_sources: PRSourcesConfig = field(default_factory=PRSourcesConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
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
        """Extract project name from CI branch prefix (e.g. 'ci/antalya' → 'antalya')."""
        parts = self.ci.branch_prefix.split("/")
        return parts[-1] if len(parts) > 1 else parts[0]

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

    def ci_branch_name(self, onto: str) -> str:
        """Build the CI branch name: ci/<base_branch_name>"""
        return f"ci/{self.base_branch_name(onto)}"

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

    ci_raw = raw.get("ci", {})
    ci = CIConfig(
        branch_prefix=ci_raw.get("branch_prefix", "ci"),
        source_branch=ci_raw.get("source_branch", ""),
        if_exists=ci_raw.get("if_exists", "skip"),
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

    raw_work_dir = raw.get("work_dir")
    work_dir = Path(raw_work_dir).resolve() if raw_work_dir else None

    return Config(
        origin=origin,
        ci=ci,
        upstream=upstream,
        features=features,
        pr_sources=pr_sources,
        notifications=notifications,
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
    }

    if config.upstream:
        data["upstream"] = {
            "remote": config.upstream.remote,
            "remote_name": config.upstream.remote_name,
        }

    data["ci"] = {
        k: v
        for k, v in {
            "branch_prefix": config.ci.branch_prefix,
            "source_branch": config.ci.source_branch,
            "if_exists": config.ci.if_exists if config.ci.if_exists != "skip" else None,
        }.items()
        if v is not None
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

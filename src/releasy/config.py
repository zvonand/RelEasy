"""Configuration loading and validation from config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class UpstreamConfig:
    remote: str
    remote_name: str = "upstream"


@dataclass
class ForkConfig:
    remote: str
    remote_name: str = "origin"


@dataclass
class CIConfig:
    branch_prefix: str  # e.g. "ci/antalya" → branches become ci/antalya/<sha8>
    source_branch: str  # initial source branch (e.g. "antalya-ci") for bootstrap
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
class NotificationsConfig:
    github_project: str | None = None


@dataclass
class Config:
    upstream: UpstreamConfig
    fork: ForkConfig
    ci: CIConfig
    features: list[FeatureConfig] = field(default_factory=list)
    pr_sources: list[PRSourceConfig] = field(default_factory=list)
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

    def get_feature_by_branch(self, branch: str) -> FeatureConfig | None:
        """Match by source_branch or by versioned branch prefix."""
        for f in self.features:
            prefix = self.feature_branch_prefix(f.id)
            if f.source_branch == branch or branch.startswith(prefix + "/"):
                return f
        return None

    def ci_branch_name(self, short_sha: str) -> str:
        """Build a versioned CI branch name: ci/<project>/<sha8>"""
        return f"{self.ci.branch_prefix}/{short_sha[:8]}"

    def feature_branch_prefix(self, feature_id: str) -> str:
        """Build the prefix for versioned feature branches: feature/<project>/<id>"""
        return f"feature/{self.project_name}/{feature_id}"

    def feature_branch_name(self, feature_id: str, short_sha: str) -> str:
        """Build a versioned feature branch name: feature/<project>/<id>/<sha8>"""
        return f"{self.feature_branch_prefix(feature_id)}/{short_sha[:8]}"


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

    upstream = UpstreamConfig(
        remote=raw["upstream"]["remote"],
        remote_name=raw["upstream"].get("remote_name", "upstream"),
    )

    fork = ForkConfig(
        remote=raw["fork"]["remote"],
        remote_name=raw["fork"].get("remote_name", "origin"),
    )

    ci_raw = raw["ci"]
    ci = CIConfig(
        branch_prefix=ci_raw["branch_prefix"],
        source_branch=ci_raw["source_branch"],
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

    pr_sources = []
    for ps_raw in raw.get("pr_sources", []):
        raw_labels = ps_raw.get("labels", [])
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        pr_sources.append(
            PRSourceConfig(
                labels=raw_labels,
                description=ps_raw.get("description", ""),
                merged_only=ps_raw.get("merged_only", False),
                auto_pr=ps_raw.get("auto_pr", False),
                if_exists=ps_raw.get("if_exists", "skip"),
            )
        )

    notifications = NotificationsConfig(
        github_project=raw.get("notifications", {}).get("github_project"),
    )

    raw_work_dir = raw.get("work_dir")
    work_dir = Path(raw_work_dir).resolve() if raw_work_dir else None

    return Config(
        upstream=upstream,
        fork=fork,
        ci=ci,
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
        "upstream": {
            "remote": config.upstream.remote,
            "remote_name": config.upstream.remote_name,
        },
        "fork": {
            "remote": config.fork.remote,
            "remote_name": config.fork.remote_name,
        },
        "ci": {
            k: v
            for k, v in {
                "branch_prefix": config.ci.branch_prefix,
                "source_branch": config.ci.source_branch,
                "if_exists": config.ci.if_exists if config.ci.if_exists != "skip" else None,
            }.items()
            if v is not None
        },
        "features": [
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
        ],
    }

    if config.work_dir:
        data["work_dir"] = str(config.work_dir)

    if config.pr_sources:
        data["pr_sources"] = [
            {
                k: v
                for k, v in {
                    "labels": ps.labels,
                    "description": ps.description or None,
                    "merged_only": ps.merged_only or None,
                    "auto_pr": ps.auto_pr or None,
                    "if_exists": ps.if_exists if ps.if_exists != "skip" else None,
                }.items()
                if v is not None
            }
            for ps in config.pr_sources
        ]

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

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


@dataclass
class FeatureConfig:
    id: str
    description: str
    source_branch: str  # existing branch where feature commits live
    enabled: bool = True
    depends_on: list[str] = field(default_factory=list)  # feature IDs this depends on (ordering TBD)


@dataclass
class PRSourceConfig:
    label: str
    description: str = ""


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
        pr_sources.append(
            PRSourceConfig(
                label=ps_raw["label"],
                description=ps_raw.get("description", ""),
            )
        )

    notifications = NotificationsConfig(
        github_project=raw.get("notifications", {}).get("github_project"),
    )

    return Config(
        upstream=upstream,
        fork=fork,
        ci=ci,
        features=features,
        pr_sources=pr_sources,
        notifications=notifications,
    )


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Persist config back to config.yaml."""
    if config_path is None:
        config_path = Path.cwd() / "config.yaml"

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
            "branch_prefix": config.ci.branch_prefix,
            "source_branch": config.ci.source_branch,
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

    if config.pr_sources:
        data["pr_sources"] = [
            {k: v for k, v in {"label": ps.label, "description": ps.description or None}.items() if v is not None}
            for ps in config.pr_sources
        ]

    if config.notifications.github_project:
        data["notifications"] = {"github_project": config.notifications.github_project}

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_github_token() -> str | None:
    return os.environ.get("RELEASY_GITHUB_TOKEN")


def get_ssh_key_path() -> str | None:
    return os.environ.get("RELEASY_SSH_KEY_PATH")


def get_repo_dir() -> Path:
    """Get the repo root directory (where config.yaml lives)."""
    return Path.cwd()

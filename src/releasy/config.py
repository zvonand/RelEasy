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
class FeatureConfig:
    id: str
    description: str
    branch: str
    enabled: bool = True


@dataclass
class NotificationsConfig:
    github_project: str | None = None  # GitHub Project URL or number (optional)


@dataclass
class Config:
    upstream: UpstreamConfig
    fork: ForkConfig
    ci_branch: str
    features: list[FeatureConfig] = field(default_factory=list)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    @property
    def enabled_features(self) -> list[FeatureConfig]:
        return [f for f in self.features if f.enabled]

    def get_feature(self, feature_id: str) -> FeatureConfig | None:
        return next((f for f in self.features if f.id == feature_id), None)

    def get_feature_by_branch(self, branch: str) -> FeatureConfig | None:
        return next((f for f in self.features if f.branch == branch), None)


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

    features = []
    for feat_raw in raw.get("features", []):
        features.append(
            FeatureConfig(
                id=feat_raw["id"],
                description=feat_raw["description"],
                branch=feat_raw["branch"],
                enabled=feat_raw.get("enabled", True),
            )
        )

    notifications = NotificationsConfig(
        github_project=raw.get("notifications", {}).get("github_project"),
    )

    return Config(
        upstream=upstream,
        fork=fork,
        ci_branch=raw["ci_branch"],
        features=features,
        notifications=notifications,
    )


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Persist config back to config.yaml."""
    if config_path is None:
        config_path = Path.cwd() / "config.yaml"

    notif: dict = {}
    if config.notifications.github_project:
        notif["github_project"] = config.notifications.github_project

    data: dict = {
        "upstream": {
            "remote": config.upstream.remote,
            "remote_name": config.upstream.remote_name,
        },
        "fork": {
            "remote": config.fork.remote,
            "remote_name": config.fork.remote_name,
        },
        "ci_branch": config.ci_branch,
        "features": [
            {
                "id": f.id,
                "description": f.description,
                "branch": f.branch,
                "enabled": f.enabled,
            }
            for f in config.features
        ],
    }
    if notif:
        data["notifications"] = notif

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def get_github_token() -> str | None:
    return os.environ.get("RELEASY_GITHUB_TOKEN")


def get_ssh_key_path() -> str | None:
    return os.environ.get("RELEASY_SSH_KEY_PATH")


def get_repo_dir() -> Path:
    """Get the repo root directory (where config.yaml lives)."""
    return Path.cwd()

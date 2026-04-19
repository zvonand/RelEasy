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


_VALID_IF_EXISTS = ("skip", "recreate")


@dataclass
class PRSourceConfig:
    labels: list[str]
    description: str = ""
    merged_only: bool = False
    # When the port branch already exists: "skip" (leave it alone) or
    # "recreate" (delete and rebuild from base). Inherits PRSourcesConfig.if_exists
    # when not set per-source.
    if_exists: str = "skip"


@dataclass
class PRGroupConfig:
    """A sequential group of PRs ported onto a single branch as one PR.

    All ``prs`` are cherry-picked, in listed order, onto the same port branch
    ``feature/<base>/<id>`` and result in a single combined PR (when push +
    pr_sources.auto_pr).
    """
    id: str
    prs: list[str]
    description: str = ""
    # When the port branch already exists locally: "skip" or "recreate".
    # Inherits from PRSourcesConfig.if_exists when not set per-group.
    if_exists: str = "skip"


@dataclass
class PRSourcesConfig:
    """PR discovery and filtering.

    Set arithmetic: union(by_labels) − exclude_labels + include_prs − exclude_prs

    ``groups`` are evaluated independently: every PR listed in any group is
    ported as part of that group (one combined PR per group), regardless of
    label matches. ``exclude_prs`` and ``exclude_labels`` still drop
    individual PRs from a group; if a group ends up empty, it is dropped
    with a warning.

    ``auto_pr`` is a single global switch: when true (the default), every
    pushed port branch (singleton, by-labels, include_prs, or group) gets a
    PR opened against the base branch. Set to false to push branches only
    and open PRs manually. Requires ``push: true`` to have any effect.
    """
    by_labels: list[PRSourceConfig] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_prs: list[str] = field(default_factory=list)
    exclude_prs: list[str] = field(default_factory=list)
    groups: list[PRGroupConfig] = field(default_factory=list)
    # Default behavior when a port branch already exists. Applied to PRs from
    # ``include_prs`` and to any ``by_labels`` / ``groups`` entry that omits
    # ``if_exists``.
    if_exists: str = "skip"
    # Global switch: open a PR for every pushed port branch.
    auto_pr: bool = True


@dataclass
class NotificationsConfig:
    github_project: str | None = None


def _default_allowed_tools() -> list[str]:
    return [
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(git:*)", "Bash(gh:*)", "Bash(cd:*)",
        "Bash(bash:*)",
        "Bash(ninja:*)", "Bash(cmake:*)", "Bash(make:*)",
        "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)",
        "Bash(tail:*)", "Bash(tee:*)", "Bash(rg:*)",
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
    # Label attached to a PR that needs human attention because the AI
    # resolver gave up (a partial-group draft PR; singletons get no PR).
    needs_attention_label: str = "ai-needs-attention"
    needs_attention_label_color: str = "D93F0B"
    extra_args: list[str] = field(default_factory=list)
    # How many times to re-invoke claude when the Anthropic streaming
    # API drops the turn with a transient error ("Stream idle timeout",
    # "Overloaded", "Connection reset", …). Each retry is a fresh turn.
    api_retries: int = 3
    api_retry_backoff_seconds: int = 15


@dataclass
class Config:
    origin: OriginConfig
    project: str  # short project identifier, e.g. "antalya"
    target_branch: str | None = None  # explicit base/target branch override
    # When a PR for the port branch already exists on GitHub:
    #   false (default) — leave it exactly as-is; don't try to create a new
    #                     one and don't touch its title/body.
    #   true            — reuse that PR but overwrite its title and body with
    #                     what releasy would have set (source PR references,
    #                     combined group body, ai-resolved prefix, …). Useful
    #                     when the source PRs' descriptions changed or you
    #                     tweaked the body format and want the rebase PR to
    #                     reflect it.
    update_existing_prs: bool = False
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
        """Build the base branch name.

        If ``target_branch`` is set in config, it is returned as-is.
        Otherwise derived as ``<project>-<version>``:

        v26.3.4.234-lts → antalya-26.3
        <sha> → antalya-<sha8>
        """
        if self.target_branch:
            return self.target_branch
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

    if "wip_commit_on_conflict" in raw:
        import logging
        logging.getLogger(__name__).warning(
            "config: 'wip_commit_on_conflict' is no longer supported and will be "
            "ignored. Unresolved conflicts now drop the local branch (singletons "
            "/ first-of-group) or open a draft PR labelled "
            "'ai-needs-attention' (partial groups), and mark the entry as "
            "'Conflict' in the GitHub Project. Remove the option from "
            "config.yaml to silence this warning."
        )

    origin = OriginConfig(
        remote=raw["origin"]["remote"],
        remote_name=raw["origin"].get("remote_name", "origin"),
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
    global_if_exists = ps_raw.get("if_exists", "skip")
    if global_if_exists not in _VALID_IF_EXISTS:
        raise ValueError(
            f"pr_sources.if_exists must be one of {_VALID_IF_EXISTS}, "
            f"got {global_if_exists!r}"
        )
    global_auto_pr = bool(ps_raw.get("auto_pr", True))
    by_labels = []
    for entry in ps_raw.get("by_labels", []):
        raw_labels = entry.get("labels", [])
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        entry_if_exists = entry.get("if_exists", global_if_exists)
        if entry_if_exists not in _VALID_IF_EXISTS:
            raise ValueError(
                f"pr_sources.by_labels[].if_exists must be one of "
                f"{_VALID_IF_EXISTS}, got {entry_if_exists!r}"
            )
        if "auto_pr" in entry:
            raise ValueError(
                f"pr_sources.by_labels[labels={raw_labels!r}].auto_pr is no "
                "longer supported. Use the global 'pr_sources.auto_pr' "
                "switch (default true) instead."
            )
        by_labels.append(
            PRSourceConfig(
                labels=raw_labels,
                description=entry.get("description", ""),
                merged_only=entry.get("merged_only", False),
                if_exists=entry_if_exists,
            )
        )
    groups: list[PRGroupConfig] = []
    seen_group_ids: set[str] = set()
    seen_group_prs: dict[str, str] = {}  # url -> group id
    for entry in ps_raw.get("groups", []):
        gid = entry.get("id")
        if not gid:
            raise ValueError("pr_sources.groups[] entries must specify 'id'")
        if gid in seen_group_ids:
            raise ValueError(f"pr_sources.groups: duplicate id {gid!r}")
        seen_group_ids.add(gid)
        prs_list = entry.get("prs", [])
        if not isinstance(prs_list, list) or len(prs_list) < 1:
            raise ValueError(
                f"pr_sources.groups[{gid!r}].prs must be a non-empty list of PR URLs"
            )
        for url in prs_list:
            if url in seen_group_prs:
                raise ValueError(
                    f"PR {url} appears in both groups {seen_group_prs[url]!r} "
                    f"and {gid!r}"
                )
            seen_group_prs[url] = gid
        group_if_exists = entry.get("if_exists", global_if_exists)
        if group_if_exists not in _VALID_IF_EXISTS:
            raise ValueError(
                f"pr_sources.groups[{gid!r}].if_exists must be one of "
                f"{_VALID_IF_EXISTS}, got {group_if_exists!r}"
            )
        if "auto_pr" in entry:
            raise ValueError(
                f"pr_sources.groups[id={gid!r}].auto_pr is no longer "
                "supported. Use the global 'pr_sources.auto_pr' switch "
                "(default true) instead."
            )
        groups.append(
            PRGroupConfig(
                id=gid,
                prs=prs_list,
                description=entry.get("description", ""),
                if_exists=group_if_exists,
            )
        )

    pr_sources = PRSourcesConfig(
        by_labels=by_labels,
        exclude_labels=ps_raw.get("exclude_labels", []),
        include_prs=ps_raw.get("include_prs", []),
        exclude_prs=ps_raw.get("exclude_prs", []),
        groups=groups,
        if_exists=global_if_exists,
        auto_pr=global_auto_pr,
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
        needs_attention_label=ai_raw.get(
            "needs_attention_label", "ai-needs-attention",
        ),
        needs_attention_label_color=ai_raw.get(
            "needs_attention_label_color", "D93F0B",
        ),
        extra_args=ai_raw.get("extra_args", []) or [],
        api_retries=int(ai_raw.get("api_retries", 3)),
        api_retry_backoff_seconds=int(ai_raw.get("api_retry_backoff_seconds", 15)),
    )

    raw_work_dir = raw.get("work_dir")
    work_dir = Path(raw_work_dir).resolve() if raw_work_dir else None

    return Config(
        origin=origin,
        project=project,
        target_branch=raw.get("target_branch") or None,
        update_existing_prs=bool(raw.get("update_existing_prs", False)),
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

    if config.target_branch:
        data["target_branch"] = config.target_branch

    if config.update_existing_prs:
        data["update_existing_prs"] = True

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
    if (
        ps.by_labels or ps.exclude_labels or ps.include_prs
        or ps.exclude_prs or ps.groups or ps.auto_pr is not True
    ):
        ps_data: dict = {}
        if ps.by_labels:
            ps_data["by_labels"] = [
                {
                    k: v
                    for k, v in {
                        "labels": entry.labels,
                        "description": entry.description or None,
                        "merged_only": entry.merged_only or None,
                        "if_exists": entry.if_exists if entry.if_exists != ps.if_exists else None,
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
        if ps.groups:
            ps_data["groups"] = [
                {
                    k: v
                    for k, v in {
                        "id": g.id,
                        "description": g.description or None,
                        "if_exists": g.if_exists if g.if_exists != ps.if_exists else None,
                        "prs": g.prs,
                    }.items()
                    if v is not None
                }
                for g in ps.groups
            ]
        if ps.if_exists != "skip":
            ps_data["if_exists"] = ps.if_exists
        if ps.auto_pr is not True:
            ps_data["auto_pr"] = ps.auto_pr
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
    if ai.needs_attention_label != ai_defaults.needs_attention_label:
        ai_data["needs_attention_label"] = ai.needs_attention_label
    if ai.needs_attention_label_color != ai_defaults.needs_attention_label_color:
        ai_data["needs_attention_label_color"] = ai.needs_attention_label_color
    if ai.extra_args:
        ai_data["extra_args"] = ai.extra_args
    if ai.api_retries != ai_defaults.api_retries:
        ai_data["api_retries"] = ai.api_retries
    if ai.api_retry_backoff_seconds != ai_defaults.api_retry_backoff_seconds:
        ai_data["api_retry_backoff_seconds"] = ai.api_retry_backoff_seconds
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

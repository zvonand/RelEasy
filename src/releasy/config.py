"""Configuration loading and validation from config.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# Slug constraints for the per-project ``name:`` field. The name doubles
# as a filename (``<name>.state.yaml``) so it must be filesystem-safe and
# short enough to be readable in ``releasy list`` output.
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STATE_SUBDIR = "releasy"


def state_root() -> Path:
    """Resolve the per-user releasy state directory (created on demand).

    Priority:

    1. ``$RELEASY_STATE_DIR`` — escape hatch for tests / CI / power users.
    2. ``$XDG_STATE_HOME/releasy`` — defaults to ``~/.local/state/releasy``
       per the XDG Base Directory spec.
    """
    override = os.environ.get("RELEASY_STATE_DIR")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        xdg = os.environ.get("XDG_STATE_HOME") or str(
            Path.home() / ".local" / "state"
        )
        root = (Path(xdg).expanduser() / _STATE_SUBDIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_file_path(name: str) -> Path:
    """Path to the per-project pipeline state file."""
    return state_root() / f"{name}.state.yaml"


def lock_file_path(name: str) -> Path:
    """Path to the per-project lock file used by ``locks.project_lock``."""
    return state_root() / f"{name}.lock"


def validate_project_name(name: str) -> str:
    """Validate ``name`` matches the slug regex; return it unchanged on success."""
    if not isinstance(name, str) or not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid project name {name!r}. Must match "
            f"{_VALID_NAME_RE.pattern} (1-64 chars, letters/digits/._-)."
        )
    return name


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

    Set arithmetic:
        union(by_labels)
        − exclude_labels − exclude_authors
        ∩ (include_authors when set)
        + include_prs
        − exclude_prs

    ``groups`` are evaluated independently: every PR listed in any group is
    ported as part of that group (one combined PR per group), regardless of
    label matches. ``exclude_prs``, ``exclude_labels``, ``include_authors``
    and ``exclude_authors`` still drop individual PRs from a group; if a
    group ends up empty, it is dropped with a warning.

    ``include_authors`` (when non-empty) restricts discovered PRs to those
    authored by one of the listed GitHub logins. ``exclude_authors`` drops
    PRs by the listed authors. Author comparisons are case-insensitive.
    Both filters are bypassed for PRs explicitly listed in ``include_prs``.

    ``auto_pr`` is a single global switch: when true (the default), every
    pushed port branch (singleton, by-labels, include_prs, or group) gets a
    PR opened against the base branch. Set to false to push branches only
    and open PRs manually. Requires ``push: true`` to have any effect.
    """
    by_labels: list[PRSourceConfig] = field(default_factory=list)
    exclude_labels: list[str] = field(default_factory=list)
    include_prs: list[str] = field(default_factory=list)
    exclude_prs: list[str] = field(default_factory=list)
    include_authors: list[str] = field(default_factory=list)
    exclude_authors: list[str] = field(default_factory=list)
    groups: list[PRGroupConfig] = field(default_factory=list)
    # Default behavior when a port branch already exists. Applied to PRs from
    # ``include_prs`` and to any ``by_labels`` / ``groups`` entry that omits
    # ``if_exists``.
    if_exists: str = "skip"
    # Global switch: open a PR for every pushed port branch.
    auto_pr: bool = True
    # Re-attempt PR units whose previous run ended in `conflict` status.
    # When true (the default), `releasy run` discards any existing local /
    # remote port branch for a conflicted entry and re-runs the cherry-pick
    # from base — useful after fixing a bug, topping up Anthropic credits,
    # or otherwise resolving whatever caused the original failure. When
    # false, conflicted entries are left exactly as they are (no new
    # cherry-pick, no PR side-effects), and `releasy run` walks past them.
    retry_failed: bool = True


def _default_assignee_dev_options() -> list[str]:
    """Single-select options for the project's ``Assignee Dev`` field.

    These are display labels (preferring each contributor's GitHub
    "name" over the bare login), kept here as a sensible team default.
    Users can override the list in ``notifications.assignee_dev_options``
    in ``config.yaml`` to add / remove team members. Whatever ends up in
    config also drives the option set RelEasy provisions on the GitHub
    Project board on first ``releasy setup-project`` run.
    """
    return [
        "Andrey Zvonov",
        "Anton Ivashkin",
        "Arthur Passos",
        "DQ",
        "Ilya Golshtein",
        "Mikhail Koviazin",
        "Vasily Nemkov",
    ]


def _default_assignee_qa_options() -> list[str]:
    """Single-select options for the project's ``Assignee QA`` field.

    ``Verified by Dev`` is a special meta-option meaning "no separate QA
    pass needed — the developer self-verified". It is intentionally not a
    real person.
    """
    return [
        "Alsu Giliazova",
        "Carlos",
        "Davit Mnatobishvili",
        "strtgbb",
        "vzakaznikov",
        "Verified by Dev",
    ]


def _default_assignee_dev_login_map() -> dict[str, str]:
    """GitHub login → ``Assignee Dev`` option label.

    Used by ``sync_project`` to seed the field with the source PR's
    author when a card is first created. Lookups are case-insensitive,
    but the original casing is preserved in this dict so it round-trips
    cleanly through ``config.yaml`` (``Enmk`` stays ``Enmk`` rather
    than being rewritten to ``enmk`` on every save). Logins that don't
    appear here leave the field empty for manual assignment.
    """
    return {
        "zvonand": "Andrey Zvonov",
        "ianton-ru": "Anton Ivashkin",
        "arthurpassos": "Arthur Passos",
        "il9ue": "DQ",
        "ilejn": "Ilya Golshtein",
        "mkmkme": "Mikhail Koviazin",
        "Enmk": "Vasily Nemkov",
    }


@dataclass
class NotificationsConfig:
    github_project: str | None = None
    # Single-select option labels for the ``Assignee Dev`` / ``Assignee QA``
    # project fields. Edit these in ``config.yaml`` to extend the team
    # roster — RelEasy provisions exactly the listed options on the
    # GitHub Project board (see ``setup_project``) and reconciles
    # missing ones on subsequent runs. Existing options the user added
    # by hand on the board are preserved (we only ever add, never
    # delete, since a different field-owner outside RelEasy may be
    # managing additions).
    assignee_dev_options: list[str] = field(
        default_factory=_default_assignee_dev_options,
    )
    assignee_qa_options: list[str] = field(
        default_factory=_default_assignee_qa_options,
    )
    # GitHub login → ``Assignee Dev`` option label. Drives the default
    # value RelEasy puts on the ``Assignee Dev`` field of newly created
    # project cards (the source PR's author). Keys are lower-cased at
    # load time so the YAML file can use whatever casing GitHub displays.
    # Logins missing from this map leave the field empty for manual
    # assignment.
    assignee_dev_login_map: dict[str, str] = field(
        default_factory=_default_assignee_dev_login_map,
    )


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
class AIChangelogConfig:
    """Claude-driven CHANGELOG entry synthesis for grouped PR ports.

    For singleton ports the changelog entry is taken verbatim from the
    source PR's own ``Changelog entry`` section — Claude is never
    invoked, no API cost, no surprises. The synthesizer only kicks in
    for multi-PR groups, where the per-PR entries can include
    intermediate fix-ups that aren't user-visible once the whole group
    is ported as a single change.

    Defaults to disabled because it costs API tokens; enable it
    explicitly when you want polished combined-port CHANGELOG entries.
    Reuses the same Claude binary (and ``ai_resolve.command`` defaults
    so users running with one resolver setup don't have to configure
    two paths) but is otherwise independent of conflict resolution.
    """
    enabled: bool = False
    command: str = "claude"
    prompt_file: str = "prompts/synthesize_changelog.md"
    timeout_seconds: int = 300  # 5 min — should be a few seconds in practice
    # Per-PR body trimmed to this many characters before being inlined
    # into the synthesis prompt. Keeps the request payload bounded for
    # groups that drag in long source-PR descriptions.
    max_pr_body_chars: int = 3000


@dataclass
class AIResolveConfig:
    """Claude-driven conflict resolver configuration."""
    enabled: bool = False
    command: str = "claude"
    prompt_file: str = "prompts/resolve_conflict.md"
    # Prompt template used when the AI resolver is driving a `git merge`
    # to completion (the ``releasy refresh`` flow that keeps already-open
    # rebase PRs current with their target branch). Kept
    # separate from ``prompt_file`` because the in-progress operation
    # ("merge" vs "cherry-pick"), the way to conclude it (`git commit
    # --no-edit` vs `git cherry-pick --continue`), and the framing of
    # what to preserve all differ.
    merge_prompt_file: str = "prompts/resolve_merge_conflict.md"
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
    name: str  # unique slug identifying this project (state file key)
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
    ai_changelog: AIChangelogConfig = field(default_factory=AIChangelogConfig)
    config_path: Path = field(default_factory=lambda: Path.cwd() / "config.yaml")
    work_dir: Path | None = None
    push: bool = False
    # Sequential mode: process the merged-time-sorted PR queue one PR per
    # invocation. Each `releasy run` / `releasy continue` either confirms
    # the previous rebase PR was merged into target_branch and ports the
    # next one, or stops with an error. Incompatible with
    # ``pr_sources.groups`` (rejected at load time).
    sequential: bool = False

    @property
    def repo_dir(self) -> Path:
        """Directory containing ``config.yaml``.

        Used as the base for resolving relative paths embedded in config
        (e.g. ``ai_resolve.prompt_file``). Pipeline state and lock files
        no longer live here — see :func:`state_root`.
        """
        return self.config_path.parent

    @property
    def state_path(self) -> Path:
        """Path to this project's state file (under the user's state dir)."""
        return state_file_path(self.name)

    @property
    def lock_path(self) -> Path:
        """Path to this project's lock file (under the user's state dir)."""
        return lock_file_path(self.name)

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

    name = raw.get("name")
    if not name:
        raise ValueError(
            "Config must set 'name:' — a unique slug identifying this "
            "project on this machine. It keys the per-project state file "
            f"under {state_root()}/<name>.state.yaml. Pick something "
            "stable like 'antalya-26.3'."
        )
    validate_project_name(name)

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
    global_retry_failed = bool(ps_raw.get("retry_failed", True))
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

    def _str_list(key: str) -> list[str]:
        """Read a list-of-strings config value, tolerating a bare string."""
        v = ps_raw.get(key, [])
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(
                f"pr_sources.{key} must be a list of strings, got {v!r}"
            )
        return v

    pr_sources = PRSourcesConfig(
        by_labels=by_labels,
        exclude_labels=ps_raw.get("exclude_labels", []),
        include_prs=ps_raw.get("include_prs", []),
        exclude_prs=ps_raw.get("exclude_prs", []),
        include_authors=_str_list("include_authors"),
        exclude_authors=_str_list("exclude_authors"),
        groups=groups,
        if_exists=global_if_exists,
        auto_pr=global_auto_pr,
        retry_failed=global_retry_failed,
    )

    notif_raw = raw.get("notifications", {}) or {}

    def _opt_list(key: str, fallback: list[str]) -> list[str]:
        v = notif_raw.get(key)
        if v is None:
            return list(fallback)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(
                f"notifications.{key} must be a list of strings, got {v!r}"
            )
        # Strip empties / dedupe while preserving order — keeps the option
        # set on the GitHub Project board predictable even when users edit
        # the YAML by hand and accidentally duplicate or comment out an
        # entry by leaving it blank.
        seen: set[str] = set()
        out: list[str] = []
        for s in v:
            stripped = s.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            out.append(stripped)
        return out

    raw_login_map = notif_raw.get("assignee_dev_login_map")
    if raw_login_map is None:
        login_map = _default_assignee_dev_login_map()
    else:
        if not isinstance(raw_login_map, dict):
            raise ValueError(
                "notifications.assignee_dev_login_map must be a "
                f"mapping (got {type(raw_login_map).__name__})"
            )
        # Preserve the user's original casing in config.yaml so save_config
        # round-trips don't silently rewrite ``Enmk`` to ``enmk``. Lookups
        # are case-insensitive — see how ``sync_project`` builds a
        # ``login_map_lc`` view at call time.
        login_map = {}
        seen_keys_lc: dict[str, str] = {}
        for k, v in raw_login_map.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    "notifications.assignee_dev_login_map entries must "
                    "be string→string"
                )
            key = k.strip()
            val = v.strip()
            if not key:
                continue
            key_lc = key.lower()
            if key_lc in seen_keys_lc:
                raise ValueError(
                    "notifications.assignee_dev_login_map has duplicate "
                    f"login (case-insensitive): {seen_keys_lc[key_lc]!r} "
                    f"and {key!r}"
                )
            seen_keys_lc[key_lc] = key
            login_map[key] = val

    notifications = NotificationsConfig(
        github_project=notif_raw.get("github_project"),
        assignee_dev_options=_opt_list(
            "assignee_dev_options", _default_assignee_dev_options(),
        ),
        assignee_qa_options=_opt_list(
            "assignee_qa_options", _default_assignee_qa_options(),
        ),
        assignee_dev_login_map=login_map,
    )

    ai_changelog_raw = raw.get("ai_changelog", {}) or {}
    ai_changelog = AIChangelogConfig(
        enabled=bool(ai_changelog_raw.get("enabled", False)),
        command=ai_changelog_raw.get("command", "claude"),
        prompt_file=ai_changelog_raw.get(
            "prompt_file", "prompts/synthesize_changelog.md",
        ),
        timeout_seconds=int(ai_changelog_raw.get("timeout_seconds", 300)),
        max_pr_body_chars=int(ai_changelog_raw.get("max_pr_body_chars", 3000)),
    )

    ai_raw = raw.get("ai_resolve", {}) or {}
    ai_resolve = AIResolveConfig(
        enabled=ai_raw.get("enabled", False),
        command=ai_raw.get("command", "claude"),
        prompt_file=ai_raw.get("prompt_file", "prompts/resolve_conflict.md"),
        merge_prompt_file=ai_raw.get(
            "merge_prompt_file", "prompts/resolve_merge_conflict.md",
        ),
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

    sequential = bool(raw.get("sequential", False))
    if sequential and groups:
        raise ValueError(
            "sequential: true is incompatible with pr_sources.groups — "
            "remove the groups or set sequential: false"
        )

    return Config(
        name=name,
        origin=origin,
        project=project,
        target_branch=raw.get("target_branch") or None,
        update_existing_prs=bool(raw.get("update_existing_prs", False)),
        features=features,
        pr_sources=pr_sources,
        notifications=notifications,
        ai_resolve=ai_resolve,
        ai_changelog=ai_changelog,
        config_path=config_path,
        work_dir=work_dir,
        push=raw.get("push", False),
        sequential=sequential,
    )


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Persist config back to config.yaml."""
    if config_path is None:
        config_path = config.config_path

    data: dict = {
        "name": config.name,
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
        or ps.exclude_prs or ps.include_authors or ps.exclude_authors
        or ps.groups or ps.auto_pr is not True or ps.retry_failed is not True
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
        if ps.include_authors:
            ps_data["include_authors"] = ps.include_authors
        if ps.exclude_authors:
            ps_data["exclude_authors"] = ps.exclude_authors
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
        if ps.retry_failed is not True:
            ps_data["retry_failed"] = ps.retry_failed
        data["pr_sources"] = ps_data

    notif_data: dict = {}
    if config.notifications.github_project:
        notif_data["github_project"] = config.notifications.github_project
    if config.notifications.assignee_dev_options != _default_assignee_dev_options():
        notif_data["assignee_dev_options"] = config.notifications.assignee_dev_options
    if config.notifications.assignee_qa_options != _default_assignee_qa_options():
        notif_data["assignee_qa_options"] = config.notifications.assignee_qa_options
    if config.notifications.assignee_dev_login_map != _default_assignee_dev_login_map():
        notif_data["assignee_dev_login_map"] = (
            config.notifications.assignee_dev_login_map
        )
    if notif_data:
        data["notifications"] = notif_data

    ai = config.ai_resolve
    ai_defaults = AIResolveConfig()
    ai_data: dict = {}
    if ai.enabled != ai_defaults.enabled:
        ai_data["enabled"] = ai.enabled
    if ai.command != ai_defaults.command:
        ai_data["command"] = ai.command
    if ai.prompt_file != ai_defaults.prompt_file:
        ai_data["prompt_file"] = ai.prompt_file
    if ai.merge_prompt_file != ai_defaults.merge_prompt_file:
        ai_data["merge_prompt_file"] = ai.merge_prompt_file
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

    aic = config.ai_changelog
    aic_defaults = AIChangelogConfig()
    aic_data: dict = {}
    if aic.enabled != aic_defaults.enabled:
        aic_data["enabled"] = aic.enabled
    if aic.command != aic_defaults.command:
        aic_data["command"] = aic.command
    if aic.prompt_file != aic_defaults.prompt_file:
        aic_data["prompt_file"] = aic.prompt_file
    if aic.timeout_seconds != aic_defaults.timeout_seconds:
        aic_data["timeout_seconds"] = aic.timeout_seconds
    if aic.max_pr_body_chars != aic_defaults.max_pr_body_chars:
        aic_data["max_pr_body_chars"] = aic.max_pr_body_chars
    if aic_data:
        data["ai_changelog"] = aic_data

    if config.push:
        data["push"] = True

    if config.sequential:
        data["sequential"] = True

    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def make_stateless_config(
    origin_url: str,
    *,
    work_dir: Path | None = None,
    push: bool = True,
    auto_pr: bool = False,
    ai_enabled: bool = False,
    ai_command: str = "claude",
    ai_build_command: str = "",
    ai_prompt_file: str | None = None,
    ai_timeout_seconds: int = 7200,
    ai_max_iterations: int = 5,
) -> Config:
    """Build an in-memory ``Config`` for the stateless cherry-pick command.

    No YAML is read or written. The resulting ``Config`` has a sentinel
    ``name`` (``"_stateless"``) — callers MUST NOT pass it to
    ``load_state`` / ``save_state`` / ``project_lock``; the stateless
    flow deliberately bypasses all per-project persistence.

    ``ai_prompt_file`` defaults to the bundled
    ``src/releasy/prompts/resolve_conflict.md`` so users running
    ``releasy cherry-pick --resolve-conflicts`` don't need to ship a
    template alongside the install.

    ``config_path`` is set to ``<cwd>/<stateless>`` so any relative-path
    resolution that goes through ``config.repo_dir`` (e.g.
    ``ai_resolve.prompt_file`` rendering) lands in a sane place even
    though no real file is read.
    """
    if ai_prompt_file is None:
        bundled = (
            Path(__file__).parent / "prompts" / "resolve_conflict.md"
        ).resolve()
        ai_prompt_file = str(bundled)

    project_slug = "stateless"
    return Config(
        name="_stateless",
        origin=OriginConfig(remote=origin_url),
        project=project_slug,
        target_branch=None,
        update_existing_prs=False,
        features=[],
        pr_sources=PRSourcesConfig(auto_pr=auto_pr),
        notifications=NotificationsConfig(),
        ai_resolve=AIResolveConfig(
            enabled=ai_enabled,
            command=ai_command,
            prompt_file=ai_prompt_file,
            build_command=ai_build_command,
            max_iterations=ai_max_iterations,
            timeout_seconds=ai_timeout_seconds,
        ),
        config_path=(Path.cwd() / "<stateless>").resolve(),
        work_dir=work_dir.resolve() if work_dir is not None else None,
        push=push,
        sequential=False,
    )


def get_github_token() -> str | None:
    return os.environ.get("RELEASY_GITHUB_TOKEN")


def get_ssh_key_path() -> str | None:
    return os.environ.get("RELEASY_SSH_KEY_PATH")

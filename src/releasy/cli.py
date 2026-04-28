"""CLI entry point using Click."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import click

from releasy import __version__
from releasy.config import (
    Config,
    state_file_path,
    state_root,
    validate_project_name,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_config_or_exit(config_path: str | None = None) -> Config:
    from releasy.config import load_config

    path = Path(config_path) if config_path else None
    try:
        return load_config(path)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to load config: {e}")


def _attach_session(
    config: Config,
    session_file_override: str | None,
    *,
    required: bool,
) -> None:
    """Populate ``config.session`` by loading the session file.

    ``required=True`` is for commands that can't do anything useful
    without features / pr_sources (``run``, ``feature *``). Missing
    session file → ``click.ClickException``.

    ``required=False`` leaves ``config.session`` as ``None`` if the file
    is missing — except when the user explicitly pointed at a specific
    path via ``--session-file``, which is always an error if absent
    (never silently fall back; the user asked for *that file*).
    """
    from releasy.config import load_session

    override = Path(session_file_override) if session_file_override else None
    if override is not None and not override.exists():
        raise click.ClickException(f"Session file not found: {override}")
    try:
        config.session = load_session(config, override)
    except FileNotFoundError as e:
        if required:
            raise click.ClickException(str(e))
        config.session = None
    except Exception as e:
        raise click.ClickException(f"Failed to load session: {e}")


def _load_and_verify(
    ctx: click.Context, *, session: str = "optional",
) -> Config:
    """Load config, verify state ownership, optionally attach session.

    ``session``: ``"required"`` (error if missing), ``"optional"``
    (leave ``config.session=None`` on missing), ``"skip"`` (don't look).

    Use this for commands that need a config but don't take the project
    lock (read-only operations, or commands that explicitly rebind state).
    """
    from releasy.state import OwnershipCollisionError, verify_ownership

    config = _load_config_or_exit(ctx.obj["config_path"])
    try:
        verify_ownership(config)
    except OwnershipCollisionError as e:
        raise click.ClickException(str(e))
    if session != "skip":
        _attach_session(
            config, ctx.obj.get("session_file"),
            required=(session == "required"),
        )
    return config


@contextmanager
def _locked_config(
    ctx: click.Context, *, session: str = "optional",
) -> Iterator[Config]:
    """Load + verify + lock a project's config; yield the Config.

    Wrap every mutating subcommand in this so concurrent invocations on
    the SAME project (same ``name:``) serialize, while invocations on
    different projects run in parallel.

    ``session`` controls session-file handling; see :func:`_load_and_verify`.
    """
    from releasy.locks import project_lock

    config = _load_and_verify(ctx, session=session)
    with project_lock(config):
        yield config


def _short_id() -> str:
    """6 hex chars from a CSPRNG — used to disambiguate auto-generated names."""
    return secrets.token_hex(3)


def _render_template(text: str, **vars: str) -> str:
    """Tiny ``{{ key }}`` substitution. Whitespace around the key is tolerated."""
    out = text
    for key, value in vars.items():
        for placeholder in (f"{{{{ {key} }}}}", f"{{{{{key}}}}}"):
            out = out.replace(placeholder, value)
    return out


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


# Click defaults `max_content_width` to 80 even on wider terminals, which
# truncates our one-line command summaries with "..." in `releasy --help`.
# Bumping it lets the help output use the full terminal width (Click takes
# `min(max_content_width, terminal_width)`), so descriptions stay readable
# at modern terminal sizes without us having to artificially shorten them.
_CLI_CONTEXT_SETTINGS = {"max_content_width": 120}


@click.group(context_settings=_CLI_CONTEXT_SETTINGS)
@click.version_option(version=__version__, prog_name="releasy")
@click.option(
    "--config",
    "--config-file",
    "config_path",
    default=None,
    help="Path to config.yaml (defaults to ./config.yaml in the current directory)",
)
@click.option(
    "--session-file",
    "session_file",
    default=None,
    help="Path to the session file (features + pr_sources). Overrides "
         "the session_file: key in config.yaml. Defaults to "
         "<config-dir>/<name>.session.yaml.",
)
@click.pass_context
def cli(
    ctx: click.Context, config_path: str | None, session_file: str | None,
) -> None:
    """RelEasy — manage port branches and release construction."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["session_file"] = session_file


# ---------------------------------------------------------------------------
# Maintenance pipeline
# ---------------------------------------------------------------------------


@cli.command(short_help="Discover + port new PRs (cherry-pick + open PR).")
@click.option(
    "--onto",
    default=None,
    help="Version label used to derive the base branch name "
         "(<project>-<version>). Just a string — never resolved as a git "
         "ref; the base branch must already exist on origin. Not needed "
         "when 'target_branch' is set in config.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    default=True,
    help="Invoke the AI resolver on conflicts (requires ai_resolve.enabled in config). "
         "Default: on.",
)
@click.option(
    "--retry-failed/--no-retry-failed",
    default=None,
    help="Re-attempt PR units whose previous run ended in `conflict` "
         "status: discard the existing branch and re-run the cherry-pick "
         "from base. With --no-retry-failed those entries are left "
         "exactly as-is. Defaults to the `pr_policy.retry_failed` value "
         "in config (true unless overridden).",
)
@click.pass_context
def run(
    ctx: click.Context,
    onto: str | None,
    work_dir: str | None,
    resolve_conflicts: bool,
    retry_failed: bool | None,
) -> None:
    """Discover and port new PRs onto the base branch (cherry-pick + open PR)."""
    from releasy.pipeline import run_pipeline, run_sequential

    with _locked_config(ctx, session="required") as config:
        if not onto:
            if not config.target_branch:
                raise click.ClickException(
                    "Either pass --onto <ref> or set 'target_branch:' in config.yaml."
                )
            onto = config.target_branch

        wd = Path(work_dir) if work_dir else None
        effective_retry_failed = (
            config.pr_policy.retry_failed
            if retry_failed is None else retry_failed
        )

        if config.sequential:
            run_sequential(
                config, onto, wd,
                resolve_conflicts=resolve_conflicts,
                retry_failed=effective_retry_failed,
            )
            return

        state = run_pipeline(
            config, onto, wd,
            resolve_conflicts=resolve_conflicts,
            retry_failed=effective_retry_failed,
        )

        has_conflicts = any(
            fs.status == "conflict" for fs in state.features.values()
        )
        if has_conflicts:
            raise SystemExit(1)


@cli.command(
    name="cherry-pick",
    short_help="One-off cross-repo cherry-pick (no config / state file).",
)
@click.option(
    "--origin",
    "origin",
    required=True,
    help="Origin remote URL (ssh or https) — the repo to clone, push to, "
         "and open the PR against. e.g. git@github.com:owner/repo.git",
)
@click.option(
    "--target",
    "target",
    required=True,
    help="Branch on origin to base the port on and (optionally) open a "
         "PR against. Must already exist on the origin remote.",
)
@click.option(
    "--commit",
    "source_url",
    required=True,
    help="GitHub URL of the source to cherry-pick. Accepts a PR "
         "(.../pull/N — uses the merge commit with -m 1), a commit "
         "(.../commit/<sha>), or a tag (.../releases/tag/<tag> or "
         ".../tree/<tag>). May reference any public repo (e.g. a fork).",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--branch-name",
    "branch_name",
    default=None,
    help="Name of the port branch. Defaults to "
         "releasy/port/<short-id>-<6hex>.",
)
@click.option(
    "--push/--no-push",
    default=True,
    help="Push the resulting branch to origin. Default: on.",
)
@click.option(
    "--with-pr",
    "with_pr",
    is_flag=True,
    default=False,
    help="Open a PR from the port branch back to --target on origin. "
         "Requires --push (implied) and RELEASY_GITHUB_TOKEN.",
)
@click.option(
    "--resolve-conflicts",
    is_flag=True,
    default=False,
    help="On conflict, invoke Claude (or whatever --claude-command points "
         "at) to resolve. Requires --build-command so Claude can verify "
         "the resolution compiles.",
)
@click.option(
    "--build-command",
    "build_command",
    default="",
    help="Shell command Claude runs to verify the resolution compiles. "
         "Required when --resolve-conflicts is set. Example: "
         "'cd build && ninja'.",
)
@click.option(
    "--claude-command",
    "claude_command",
    default="claude",
    show_default=True,
    help="Executable used to invoke Claude.",
)
@click.option(
    "--prompt-file",
    "prompt_file",
    default=None,
    help="Path to the AI-resolve prompt template. Defaults to the "
         "prompt bundled with the releasy package.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=7200,
    show_default=True,
    help="Per-attempt Claude timeout (seconds).",
)
@click.option(
    "--max-iterations",
    "max_iterations",
    type=int,
    default=5,
    show_default=True,
    help="Maximum build attempts Claude may make per resolve invocation.",
)
def cherry_pick_cmd(
    origin: str,
    target: str,
    source_url: str,
    work_dir: str | None,
    branch_name: str | None,
    push: bool,
    with_pr: bool,
    resolve_conflicts: bool,
    build_command: str,
    claude_command: str,
    prompt_file: str | None,
    timeout_seconds: int,
    max_iterations: int,
) -> None:
    """One-off cross-repo cherry-pick — no config file, no state file.

    Cherry-picks a PR / commit / tag from any public GitHub repo onto a
    fresh branch off ``--target`` in ``--origin``, optionally lets
    Claude resolve any conflicts, optionally pushes the branch and
    opens a PR back against ``--target``.

    Nothing is persisted: this command does not read or write any
    releasy config / state / lock / project board. Re-running it makes
    a brand-new branch every time (use ``--branch-name`` to control it).
    """
    if resolve_conflicts and not build_command.strip():
        raise click.UsageError(
            "--resolve-conflicts requires --build-command (the shell "
            "command Claude will run to verify the resolution compiles). "
            "Pass --build-command 'cd build && ninja' (or similar)."
        )
    if with_pr and not push:
        raise click.UsageError(
            "--with-pr requires --push; cannot open a PR for an "
            "unpushed branch."
        )

    from releasy.stateless import StatelessOptions, run_stateless_cherry_pick

    opts = StatelessOptions(
        origin=origin,
        target=target,
        source_url=source_url,
        work_dir=Path(work_dir) if work_dir else None,
        branch_name=branch_name,
        push=push,
        open_pr=with_pr,
        resolve_conflicts=resolve_conflicts,
        build_command=build_command,
        claude_command=claude_command,
        prompt_file=prompt_file,
        timeout_seconds=timeout_seconds,
        max_iterations=max_iterations,
    )

    result = run_stateless_cherry_pick(opts)
    if not result.success:
        if result.error:
            raise click.ClickException(result.error)
        raise SystemExit(1)


@cli.command(
    name="continue",
    short_help="Reconcile state after manual fixes.",
)
@click.option(
    "--branch",
    default=None,
    help="Branch or feature ID to mark resolved. If omitted, reconciles "
         "every port in state: opens PRs for any clean branch that lacks "
         "one (e.g. previous run had auto_pr off), pushes + opens PRs for "
         "newly-resolved conflicts, highlights any still-unresolved ones, "
         "and refreshes the GitHub Project board.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def continue_cmd(ctx: click.Context, branch: str | None, work_dir: str | None) -> None:
    """Reconcile state after a manual fix (push + open any missing PRs)."""
    from releasy.pipeline import continue_all, continue_branch, run_sequential

    with _locked_config(ctx, session="optional") as config:
        wd = Path(work_dir) if work_dir else None
        if branch:
            if not continue_branch(config, branch):
                raise SystemExit(1)
            return

        if config.sequential:
            if not config.target_branch:
                raise click.ClickException(
                    "Sequential mode requires 'target_branch:' to be set in config.yaml."
                )
            run_sequential(
                config, config.target_branch, wd, resolve_conflicts=True,
            )
            return

        if not continue_all(config, wd):
            raise SystemExit(1)


@cli.command(short_help="Mark a port as skipped (state-only).")
@click.option("--branch", required=True, help="Branch name or feature ID to skip")
@click.pass_context
def skip(ctx: click.Context, branch: str) -> None:
    """Mark a port branch as skipped (state-only, branch + PR untouched)."""
    from releasy.pipeline import skip_branch

    with _locked_config(ctx, session="skip") as config:
        if not skip_branch(config, branch):
            raise SystemExit(1)


@cli.command(short_help="Persist state and exit (nothing rolled back).")
@click.pass_context
def abort(ctx: click.Context) -> None:
    """Persist current state and exit (no rollback; branches/PRs untouched)."""
    from releasy.pipeline import abort_run

    with _locked_config(ctx, session="skip") as config:
        abort_run(config)


@cli.command(short_help="Print current pipeline state (read-only).")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Print current pipeline state (read-only, no git/network)."""
    from releasy.pipeline import print_status

    config = _load_and_verify(ctx, session="skip")
    print_status(config)


@cli.command(
    name="import",
    short_help="Rebuild project state from GitHub + the project board.",
)
@click.pass_context
def import_cmd(ctx: click.Context) -> None:
    """Rebuild the per-project state file from GitHub + the GitHub Project board.

    Use this when the local state is missing or out of date (fresh
    machine, teammate takeover, throwaway CI runner) but the rest of the
    world is unchanged — source PRs still live on GitHub, rebase PRs are
    still open on origin, and the configured project board still carries
    the Skipped / AI Cost history.

    Read-only on git: no checkouts, no clones, no pushes, no new PRs.
    The command only hits the GitHub REST / GraphQL APIs. It merges into
    any existing state file — local-only fields (ai_iterations,
    failed_step_index, partial_pr_count) are preserved verbatim; the
    board wins for `Skipped` decisions and `AI Cost`; every other field
    is refreshed from the authoritative source.

    Requires notifications.github_project in config — without a project
    board there's no durable source for the Skipped / cost values that
    can't be re-derived from PRs alone.
    """
    from releasy.import_state import import_from_github

    with _locked_config(ctx, session="optional") as config:
        if not import_from_github(config):
            raise SystemExit(1)


@cli.command(short_help="Merge target branch into each tracked PR.")
@click.option(
    "--work-dir", default=None, help="Working directory for git operations",
)
@click.option(
    "--resolve-conflicts/--no-resolve-conflicts",
    default=True,
    help="Invoke the AI resolver on merge conflicts (requires "
         "ai_resolve.enabled in config). With --no-resolve-conflicts, "
         "any tracked PR that conflicts with the target branch is just "
         "flagged in state without attempting an automatic fix. "
         "Default: on.",
)
@click.pass_context
def refresh(
    ctx: click.Context, work_dir: str | None, resolve_conflicts: bool,
) -> None:
    """Merge target branch into each tracked PR (AI-resolves conflicts).

    Strictly a maintenance pass — never opens new PRs, never creates
    new branches, never discovers new PR sources. Only operates on
    entries already present in the project state file.

    For every PR tracked in state that has a rebase PR open and a
    branch on origin: fetch the latest target + PR branch, attempt
    ``git merge --no-ff origin/<base>`` into the PR branch, and:

    - clean merge: leave the PR alone (use GitHub's *Update branch* if
      you want a fresh merge commit pushed),
    - conflict + AI resolves it: push the resolved merge commit
      (status preserved, ``ai_resolved`` flag set),
    - conflict + AI gives up / disabled: abort the merge, hard-reset
      the local branch back to its original tip, mark the entry as
      ``conflict`` in state + project board.

    Exit code is 1 if any PR ended up in conflict status, 0 otherwise —
    suitable for cron / CI loops.
    """
    from releasy.refresh import refresh_tracked_prs

    with _locked_config(ctx, session="skip") as config:
        wd = Path(work_dir) if work_dir else None
        if not refresh_tracked_prs(config, wd, resolve_conflicts=resolve_conflicts):
            raise SystemExit(1)


@cli.command(
    name="address-review",
    short_help="AI-address reviewer feedback on a PR (stateless).",
)
@click.option(
    "--pr",
    "pr_url",
    required=True,
    help="GitHub URL of the PR to address. Must live on the project's "
         "configured origin (the head branch has to be on origin so we "
         "can push to it).",
)
@click.option(
    "--reviewer",
    "cli_reviewers",
    multiple=True,
    help="GitHub login (repeatable). ADDS to "
         "review_response.trusted_reviewers from config — an explicit "
         "--reviewer entry is authoritative on its own (the login does "
         "NOT need to appear in the config allowlist first). Only "
         "comments authored by a login in the resulting set are fed to "
         "the AI; every other comment is dropped before the prompt is "
         "rendered. Case-insensitive. The command only refuses to run "
         "when BOTH this flag and the config list are empty.",
)
@click.option(
    "--since",
    "since_iso",
    default=None,
    help="Only consider comments newer than this reference. Accepts "
         "two forms: (1) a GitHub comment URL "
         "(e.g. https://github.com/o/r/pull/123#issuecomment-456, or "
         "`#discussion_r<id>` / `#pullrequestreview-<id>`) — interpreted "
         "as 'every comment STRICTLY AFTER this one'; (2) an ISO-8601 "
         "timestamp (e.g. 2026-04-24T10:00:00Z) — interpreted as "
         "'comments at or after this moment'. When omitted, RelEasy "
         "also checks the state file: if this PR matches a tracked "
         "rebase PR with a prior address-review run, the timestamp "
         "recorded then is used as an implicit exclusive --since "
         "default.",
)
@click.option(
    "--work-dir", default=None, help="Working directory for git operations",
)
@click.option(
    "--reply/--no-reply",
    "reply_to_non_addressable",
    default=None,
    help="Post a reply in-thread on every comment the AI classifies as "
         "non-actionable (already fixed / out of scope / "
         "misunderstanding). Replies carry a bot footer. When omitted, "
         "inherits from review_response.reply_to_non_addressable in "
         "config (default on). Use --no-reply for a silent run that "
         "only reports via the AI's terminal narration.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Fetch and print the filtered comment list, then exit without "
         "invoking the AI or pushing anything. Useful to verify your "
         "reviewer allowlist catches the right people.",
)
@click.option(
    "--stateless",
    is_flag=True,
    default=False,
    help="Skip the session and state files: no per-project lock, no "
         "ownership check, no state mutations. config.yaml IS still "
         "loaded (with the usual --config override) so AI settings, "
         "origin, trusted_reviewers, etc. are inherited from it. The "
         "--origin / --build-command / --claude-command / --prompt-file "
         "/ --timeout / --max-iterations / --post-summary-comment "
         "overrides below apply only with --stateless. When no "
         "config.yaml is present in cwd (and --config is not passed), "
         "a synthetic config is built from the flags; --origin defaults "
         "to the PR's host repo as an https URL in that case.",
)
@click.option(
    "--origin",
    "origin_url",
    default=None,
    help="(stateless only) Origin remote URL to push to. Use this if "
         "you need an ssh-form URL (e.g. git@github.com:owner/repo.git) "
         "instead of https. When config.yaml is present its origin is "
         "used unless this flag overrides.",
)
@click.option(
    "--build-command",
    "build_command_cli",
    default=None,
    help="(stateless only) Shell command the AI may run inside the "
         "repo to verify its changes compile. Written into "
         ".releasy/build.sh. Empty means 'no build — AI skips "
         "verification'. Overrides ai_resolve.build_command in config.",
)
@click.option(
    "--claude-command",
    "claude_command",
    default=None,
    help="(stateless only) Executable used to invoke Claude. "
         "Overrides review_response.command in config.",
)
@click.option(
    "--prompt-file",
    "prompt_file_cli",
    default=None,
    help="(stateless only) Path to the address-review prompt template. "
         "Overrides review_response.prompt_file in config. Defaults to "
         "the prompt bundled with the releasy package.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help="(stateless only) Per-invocation Claude timeout in seconds. "
         "Overrides review_response.timeout_seconds in config.",
)
@click.option(
    "--max-iterations",
    "max_iterations_cli",
    type=int,
    default=None,
    help="(stateless only) Hard cap on build attempts per run. "
         "Overrides review_response.max_iterations in config.",
)
@click.option(
    "--post-summary-comment",
    "post_summary_comment_cli",
    is_flag=True,
    default=False,
    help="(stateless only) Also post one top-level summary comment on "
         "the PR when done (distinct from per-comment replies).",
)
@click.pass_context
def address_review_cmd(
    ctx: click.Context,
    pr_url: str,
    cli_reviewers: tuple[str, ...],
    since_iso: str | None,
    reply_to_non_addressable: bool | None,
    work_dir: str | None,
    dry_run: bool,
    stateless: bool,
    origin_url: str | None,
    build_command_cli: str | None,
    claude_command: str | None,
    prompt_file_cli: str | None,
    timeout_seconds: int | None,
    max_iterations_cli: int | None,
    post_summary_comment_cli: bool,
) -> None:
    """Let the AI address review feedback on a PR.

    Fetches every comment on the PR (issue comments, inline review
    comments, review bodies) and **filters them down to comments by
    trusted reviewers** — the allowlist comes from
    ``review_response.trusted_reviewers`` in config plus any
    ``--reviewer`` flags. The AI only ever sees the filtered list, so
    an untrusted commenter can't smuggle instructions into the run.

    After filtering, the PR's head branch is checked out locally and
    Claude is asked to address the feedback by appending new commits
    (history stays linear — no amend, no rebase, no revert-via-reset).
    Successful runs push the branch with a plain ``git push`` (no
    force); a race with the PR author aborts without clobbering their
    work.

    Works on any PR on origin — the PR does not need to be tracked in
    state. When it **is** tracked (i.e. RelEasy opened this rebase PR
    itself), successful runs stamp ``last_review_addressed_at`` on the
    feature; the next invocation uses it as an implicit exclusive
    --since default so re-runs only pick up new feedback. For
    untracked PRs, pass ``--since`` explicitly (URL or ISO) when you
    want incremental behaviour.

    Exit code is 1 on any failure (missing allowlist, Claude failed,
    push race, non-linear history detected, …); 0 on success or when
    there were no trusted comments to address.
    """
    from releasy.review_response import address_review

    wd = Path(work_dir) if work_dir else None

    # Stateless-only flags — using them without --stateless is a user
    # error rather than a silent fallback to config, so the intent is
    # unambiguous on both sides.
    stateless_only_set: list[str] = []
    if origin_url is not None:
        stateless_only_set.append("--origin")
    if build_command_cli is not None:
        stateless_only_set.append("--build-command")
    if claude_command is not None:
        stateless_only_set.append("--claude-command")
    if prompt_file_cli is not None:
        stateless_only_set.append("--prompt-file")
    if timeout_seconds is not None:
        stateless_only_set.append("--timeout")
    if max_iterations_cli is not None:
        stateless_only_set.append("--max-iterations")
    if post_summary_comment_cli:
        stateless_only_set.append("--post-summary-comment")

    if not stateless and stateless_only_set:
        raise click.UsageError(
            f"{', '.join(stateless_only_set)} only apply with "
            "--stateless. Drop the flags, or add --stateless to skip "
            "the session/state/lock layer."
        )

    if stateless:
        from releasy.config import (
            build_stateless_address_review_config,
            load_config,
            overlay_address_review_overrides,
        )
        from releasy.github_ops import parse_pr_url, slug_to_https_url

        # Try to load config.yaml via the usual --config resolution; fall
        # back to a fully synthetic config only when no file is found.
        config_path = ctx.obj.get("config_path")
        config: Config | None = None
        try:
            config = load_config(
                Path(config_path) if config_path else None,
            )
        except FileNotFoundError:
            config = None
        except Exception as e:
            raise click.ClickException(f"Failed to load config: {e}")

        if config is None:
            effective_origin = origin_url
            if not effective_origin:
                parsed = parse_pr_url(pr_url)
                if parsed is None:
                    raise click.ClickException(
                        f"Could not parse --pr URL: {pr_url!r}"
                    )
                owner, repo, _ = parsed
                effective_origin = slug_to_https_url(f"{owner}/{repo}")
            config = build_stateless_address_review_config(
                origin_url=effective_origin,
                work_dir=wd,
                claude_command=claude_command or "claude",
                build_command=build_command_cli or "",
                prompt_file=prompt_file_cli,
                timeout_seconds=(
                    timeout_seconds if timeout_seconds is not None else 7200
                ),
                max_iterations=(
                    max_iterations_cli
                    if max_iterations_cli is not None else 15
                ),
                trusted_reviewers=list(cli_reviewers),
                reply_to_non_addressable=(
                    True if reply_to_non_addressable is None
                    else reply_to_non_addressable
                ),
                post_summary_comment=post_summary_comment_cli,
            )
        else:
            # Real config loaded — overlay only the flags the user passed.
            # Origin override is applied directly on the OriginConfig; the
            # rest go through the helper.
            if origin_url:
                config.origin.remote = origin_url
            overlay_address_review_overrides(
                config,
                claude_command=claude_command,
                build_command=build_command_cli,
                prompt_file=prompt_file_cli,
                timeout_seconds=timeout_seconds,
                max_iterations=max_iterations_cli,
                # --reviewer flags ADD to config's trusted_reviewers.
                trusted_reviewers=(
                    list(config.review_response.trusted_reviewers)
                    + list(cli_reviewers)
                ) if cli_reviewers else None,
                reply_to_non_addressable=reply_to_non_addressable,
                post_summary_comment=(
                    True if post_summary_comment_cli else None
                ),
            )

        # Stateless: never load state, never lock, never attach session.
        config.session = None
        config.stateless = True
        result = address_review(
            config,
            pr_url,
            cli_reviewers=cli_reviewers,
            since_iso=since_iso,
            work_dir=wd,
            dry_run=dry_run,
            reply_override=(
                None if reply_to_non_addressable is None
                else reply_to_non_addressable
            ),
        )
        if not result.success:
            if result.error:
                raise click.ClickException(result.error)
            raise SystemExit(1)
        return

    with _locked_config(ctx, session="skip") as config:
        result = address_review(
            config,
            pr_url,
            cli_reviewers=cli_reviewers,
            since_iso=since_iso,
            work_dir=wd,
            dry_run=dry_run,
            reply_override=reply_to_non_addressable,
        )
        if not result.success:
            if result.error:
                raise click.ClickException(result.error)
            raise SystemExit(1)


@cli.command(
    name="analyze-fails",
    short_help="Investigate failed CI tests on a PR (or every tracked PR).",
)
@click.option(
    "--pr",
    "pr_url",
    default=None,
    help="GitHub URL of the PR to analyse. When omitted, every PR "
         "RelEasy currently tracks in state (with a rebase_pr_url) is "
         "processed in turn.",
)
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Discover failed tests, build the flaky-elsewhere map, and "
         "print what would happen — without invoking Claude or pushing.",
)
@click.option(
    "--push/--no-push",
    default=True,
    help="Push commits the AI appended to each PR's head branch. "
         "Default: on.",
)
@click.option(
    "--no-flaky-check",
    is_flag=True,
    default=False,
    help="Skip the flaky-elsewhere assessment (don't fetch reports for "
         "other tracked PRs to corroborate flake signals). Faster, "
         "but Claude has to judge unrelated-vs-related from the diff "
         "alone.",
)
@click.option(
    "--post-comment/--no-post-comment",
    "post_comment",
    default=None,
    help="Post a top-level summary comment on each processed PR "
         "(per-shard outcomes, AI narration, commit + push status). "
         "Defaults to the `analyze_fails.post_comment_to_pr` config "
         "value (default: on). Use --no-post-comment for silent "
         "runs that only narrate to local stdout.",
)
@click.option(
    "--stateless",
    is_flag=True,
    default=False,
    help="Skip the session and state files: no per-project lock, no "
         "ownership check, no state mutations. config.yaml IS still "
         "loaded (with the usual --config override) so AI settings, "
         "origin, etc. are inherited. Required if --pr points at a "
         "repo that's not the project origin, or when no project "
         "config exists in cwd.",
)
@click.option(
    "--origin",
    "origin_url",
    default=None,
    help="(stateless only) Origin remote URL to push to. Use this if "
         "you need an ssh-form URL instead of https. When config.yaml "
         "is present its origin is used unless this flag overrides.",
)
@click.option(
    "--build-command",
    "build_command_cli",
    default=None,
    help="(stateless only) Shell command the AI may run inside the "
         "repo to verify its changes compile and to (re)build "
         "ClickHouse before reproducing the failing test. Empty means "
         "'no build'. Overrides ai_resolve.build_command in config.",
)
@click.option(
    "--claude-command",
    "claude_command",
    default=None,
    help="(stateless only) Executable used to invoke Claude. "
         "Overrides analyze_fails.command in config.",
)
@click.option(
    "--prompt-file",
    "prompt_file_cli",
    default=None,
    help="(stateless only) Path to the analyze-fails prompt template. "
         "Overrides analyze_fails.prompt_file in config. Defaults to "
         "the prompt bundled with the releasy package.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=None,
    help="(stateless only) Per-invocation Claude timeout in seconds.",
)
@click.option(
    "--max-iterations",
    "max_iterations_cli",
    type=int,
    default=None,
    help="(stateless only) Hard cap on build attempts per failed test.",
)
@click.option(
    "--max-prs",
    "max_prs_cli",
    type=int,
    default=None,
    help="(stateless only) Cap on how many tracked PRs to process when "
         "--pr is omitted (0 = no cap). Overrides "
         "analyze_fails.max_prs_per_run.",
)
@click.pass_context
def analyze_fails_cmd(
    ctx: click.Context,
    pr_url: str | None,
    work_dir: str | None,
    dry_run: bool,
    push: bool,
    no_flaky_check: bool,
    post_comment: bool | None,
    stateless: bool,
    origin_url: str | None,
    build_command_cli: str | None,
    claude_command: str | None,
    prompt_file_cli: str | None,
    timeout_seconds: int | None,
    max_iterations_cli: int | None,
    max_prs_cli: int | None,
) -> None:
    """Walk failed CI on a PR (or every tracked PR), debug + fix per test.

    For each failed test that surfaces in a praktika JSON report
    (Fast test / Stateless tests / Integration tests), Claude:

    \b
    1. Reads the failure excerpt and the PR's diff.
    2. Decides "related to this PR" vs "unrelated flake on master".
    3. If related, reproduces the failure locally, fixes the test or
       the code under test, and commits.

    A "flaky-elsewhere" assessment is built from the OTHER tracked
    PRs' reports — when a test is failing in N >= threshold other PRs,
    Claude is told so and is encouraged to exit with UNRELATED. The
    final classification is always Claude's call (the heuristic is a
    hint, not a hard cutoff), so disable it with --no-flaky-check if
    you want every test investigated regardless.

    Exit code is 1 on any per-PR failure (couldn't fetch metadata,
    push race, non-linear history, …); 0 on success even if every
    test was UNRELATED.
    """
    from releasy.analyze_fails import analyze_fails

    wd = Path(work_dir) if work_dir else None

    stateless_only_set: list[str] = []
    if origin_url is not None:
        stateless_only_set.append("--origin")
    if build_command_cli is not None:
        stateless_only_set.append("--build-command")
    if claude_command is not None:
        stateless_only_set.append("--claude-command")
    if prompt_file_cli is not None:
        stateless_only_set.append("--prompt-file")
    if timeout_seconds is not None:
        stateless_only_set.append("--timeout")
    if max_iterations_cli is not None:
        stateless_only_set.append("--max-iterations")
    if max_prs_cli is not None:
        stateless_only_set.append("--max-prs")

    if not stateless and stateless_only_set:
        raise click.UsageError(
            f"{', '.join(stateless_only_set)} only apply with "
            "--stateless. Drop the flags, or add --stateless to skip "
            "the session/state/lock layer."
        )

    if stateless:
        from releasy.config import (
            build_stateless_analyze_fails_config,
            load_config,
            overlay_analyze_fails_overrides,
        )
        from releasy.github_ops import parse_pr_url, slug_to_https_url

        config_path = ctx.obj.get("config_path")
        config: Config | None = None
        try:
            config = load_config(
                Path(config_path) if config_path else None,
            )
        except FileNotFoundError:
            config = None
        except Exception as e:
            raise click.ClickException(f"Failed to load config: {e}")

        if config is None:
            effective_origin = origin_url
            if not effective_origin:
                if not pr_url:
                    raise click.ClickException(
                        "Stateless run without config.yaml requires "
                        "either --origin or --pr (so the origin can be "
                        "derived from the PR URL)."
                    )
                parsed = parse_pr_url(pr_url)
                if parsed is None:
                    raise click.ClickException(
                        f"Could not parse --pr URL: {pr_url!r}"
                    )
                owner, repo, _ = parsed
                effective_origin = slug_to_https_url(f"{owner}/{repo}")
            config = build_stateless_analyze_fails_config(
                origin_url=effective_origin,
                work_dir=wd,
                claude_command=claude_command or "claude",
                build_command=build_command_cli or "",
                prompt_file=prompt_file_cli,
                timeout_seconds=(
                    timeout_seconds if timeout_seconds is not None else 7200
                ),
                max_iterations=(
                    max_iterations_cli
                    if max_iterations_cli is not None else 6
                ),
                max_prs_per_run=(
                    max_prs_cli if max_prs_cli is not None else 0
                ),
            )
        else:
            if origin_url:
                config.origin.remote = origin_url
            overlay_analyze_fails_overrides(
                config,
                claude_command=claude_command,
                build_command=build_command_cli,
                prompt_file=prompt_file_cli,
                timeout_seconds=timeout_seconds,
                max_iterations=max_iterations_cli,
                max_prs_per_run=max_prs_cli,
            )

        config.session = None
        config.stateless = True
        result = analyze_fails(
            config, pr_url=pr_url, work_dir=wd, dry_run=dry_run,
            push=push, no_flaky_check=no_flaky_check,
            post_comment=post_comment,
        )
        if not result.success:
            if result.error:
                raise click.ClickException(result.error)
            raise SystemExit(1)
        for r in result.runs:
            if r.comment_url:
                click.echo(f"PR comment: {r.comment_url}")
        return

    if pr_url is None:
        # Multi-PR mode needs the state file to enumerate tracked PRs.
        with _locked_config(ctx, session="optional") as config:
            result = analyze_fails(
                config, pr_url=None, work_dir=wd, dry_run=dry_run,
                push=push, no_flaky_check=no_flaky_check,
            )
            if not result.success:
                if result.error:
                    raise click.ClickException(result.error)
                raise SystemExit(1)
        return

    with _locked_config(ctx, session="skip") as config:
        result = analyze_fails(
            config, pr_url=pr_url, work_dir=wd, dry_run=dry_run,
            push=push, no_flaky_check=no_flaky_check,
            post_comment=post_comment,
        )
        if not result.success:
            if result.error:
                raise click.ClickException(result.error)
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


@cli.command(short_help="Build a release branch from a tag.")
@click.option(
    "--base-tag", "base_tag", required=True,
    help="Tag/ref to base the release on (must be present locally or "
         "fetchable from origin)",
)
@click.option("--name", required=True, help="Release branch name")
@click.option("--strict", is_flag=True, help="Abort if any enabled feature is not ok")
@click.option("--include-skipped", is_flag=True, help="Include skipped features in release")
@click.option("--work-dir", default=None, help="Working directory for git operations")
@click.pass_context
def release(
    ctx: click.Context,
    base_tag: str,
    name: str,
    strict: bool,
    include_skipped: bool,
    work_dir: str | None,
) -> None:
    """Build a release branch from a tag, merging finished ports onto it."""
    from releasy.release import build_release

    with _locked_config(ctx, session="optional") as config:
        wd = Path(work_dir) if work_dir else None
        if not build_release(config, base_tag, name, strict, include_skipped, wd):
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------------


@cli.command(
    name="setup-project",
    short_help="Create or verify the GitHub Project board.",
)
@click.pass_context
def setup_project_cmd(ctx: click.Context) -> None:
    """Create or verify a GitHub Project for status tracking.

    If notifications.github_project is set in config, verifies the project
    and its Status field. Otherwise, creates a new project and prints the URL
    to add to config.

    The Status field is fully owned by RelEasy: any options that aren't
    in the canonical set (Needs Review, Branch Created, Conflict,
    Skipped) get dropped on every run. After dropping orphan options,
    this command also triggers a project sync so any cards that were
    sitting on a now-removed option get re-assigned to the right Status
    based on local state.
    """
    from releasy.github_ops import setup_project
    from releasy.pipeline import sync_to_project

    with _locked_config(ctx, session="skip") as config:
        url = setup_project(config)
        if not url:
            raise SystemExit(1)
        click.echo(f"Project ready: {url}")
        if not config.notifications.github_project:
            click.echo(
                f"\nAdd this to your config.yaml:\n\n"
                f"notifications:\n"
                f"  github_project: {url}\n"
            )
            return
        sync_to_project(config)


@cli.command(
    name="sync-project",
    short_help="Push local state to the Project board.",
)
@click.pass_context
def sync_project_cmd(ctx: click.Context) -> None:
    """Push the current local state to the GitHub Project board.

    Reads the per-project state file and reconciles every known feature
    with the configured project: attaches any missing PR cards, refreshes
    existing ones, and updates their Status field. No git operations,
    no PRs — just the project board.
    """
    from releasy.pipeline import sync_to_project

    with _locked_config(ctx, session="optional") as config:
        if not sync_to_project(config):
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# Multi-project ergonomics
# ---------------------------------------------------------------------------


@cli.command(name="new", short_help="Scaffold a fresh config from the bundled template.")
@click.option(
    "--name",
    "name_opt",
    default=None,
    help="Project name (slug). Auto-generated from --target-branch + a 6-hex "
         "id when omitted.",
)
@click.option(
    "--target-branch",
    "target_branch",
    default=None,
    help="Target/base branch this project will port onto (e.g. antalya-26.3). "
         "Used to seed `target_branch:` in the config and, when --name is "
         "omitted, to derive the auto-generated name (`<target-branch>-<6hex>`).",
)
@click.option(
    "--project",
    "project_opt",
    default="",
    help="Short project identifier used in derived branch names "
         "(e.g. antalya). Left blank when not given so you can fill it in.",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False, file_okay=True, path_type=Path),
    help="Where to write the new config. Defaults to ./config.yaml; refuses "
         "to overwrite an existing file.",
)
def new_cmd(
    name_opt: str | None,
    target_branch: str | None,
    project_opt: str,
    out_path: Path | None,
) -> None:
    """Scaffold a new releasy config and print its absolute path.

    Prints ONLY the absolute path on stdout, so it composes:

        cd $(dirname "$(releasy new --target-branch antalya-25.8)")
    """
    if name_opt is None:
        suffix = _short_id()
        if target_branch:
            name_opt = f"{target_branch}-{suffix}"
        else:
            name_opt = f"releasy-{suffix}"
    try:
        validate_project_name(name_opt)
    except ValueError as e:
        raise click.ClickException(str(e))

    if out_path is None:
        out_path = Path.cwd() / "config.yaml"
    out_path = out_path.expanduser().resolve()
    if out_path.exists():
        raise click.ClickException(
            f"{out_path} already exists. Pass --out to write somewhere else "
            f"or remove the existing file first."
        )

    templates_dir = Path(__file__).parent / "templates"
    config_tmpl = templates_dir / "config.yaml.tmpl"
    session_tmpl = templates_dir / "session.yaml.tmpl"
    if not config_tmpl.exists() or not session_tmpl.exists():
        raise click.ClickException(
            f"Bundled templates missing under {templates_dir}. This is a "
            "packaging bug — please report it."
        )

    # Session file goes next to config.yaml as ``<name>.session.yaml`` —
    # keep it co-located so users editing config see the session file
    # right there.
    session_path = out_path.parent / f"{name_opt}.session.yaml"
    if session_path.exists():
        raise click.ClickException(
            f"{session_path} already exists. Remove it or pass --out to "
            "write somewhere else."
        )

    rendered_config = _render_template(
        config_tmpl.read_text(),
        name=name_opt,
        target_branch=target_branch or "",
        project=project_opt or "",
    )
    rendered_session = session_tmpl.read_text()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered_config)
    session_path.write_text(rendered_session)

    # stdout is the absolute path of config.yaml and nothing else; user-
    # facing chatter goes to stderr so shell composition
    # (cd $(releasy new …)) works.
    click.echo(str(out_path))
    click.echo(
        f"Created config for project {name_opt!r}.\n"
        f"  Config : {out_path}\n"
        f"  Session: {session_path}\n"
        f"  State  : {state_file_path(name_opt)}",
        err=True,
    )


@cli.command(name="list", short_help="List every releasy project on this machine.")
def list_cmd() -> None:
    """List every project found under the user's state dir.

    Outputs one row per project: ``name | phase | features | last_run | config``.
    """
    from rich.table import Table

    from releasy.termlog import console

    from releasy.state import _read_raw_state  # internal helper, see state.py

    root = state_root()
    state_files = sorted(root.glob("*.state.yaml"))
    if not state_files:
        click.echo(f"No projects found under {root}.")
        click.echo(
            "Use `releasy new` to scaffold one, then `releasy run` to "
            "kick off the pipeline."
        )
        return

    table = Table(title=f"RelEasy projects ({root})", title_justify="left")
    table.add_column("Name", style="cyan")
    table.add_column("Phase")
    table.add_column("Features")
    table.add_column("Last run")
    table.add_column("Config")

    for path in state_files:
        name = path.name[: -len(".state.yaml")]
        raw = _read_raw_state(path)
        run_blob = raw.get("last_run") or {}
        phase = run_blob.get("phase") or "—"
        features = (run_blob.get("features") or {})
        ok = sum(
            1 for f in features.values()
            if (f or {}).get("status") in ("needs_review", "branch_created")
        )
        conflict = sum(
            1 for f in features.values()
            if (f or {}).get("status") == "conflict"
        )
        skipped = sum(
            1 for f in features.values()
            if (f or {}).get("status") == "skipped"
        )
        feat_summary = (
            f"{ok} ok / {conflict} conflict"
            + (f" / {skipped} skipped" if skipped else "")
        )
        table.add_row(
            name,
            phase,
            feat_summary,
            run_blob.get("started_at") or "—",
            raw.get("config_path") or "—",
        )
    console.print(table)


# Register `releasy ls` as an alias for `releasy list`. We add it as a
# separate Click command (rather than `aliases=`, which Click doesn't
# support natively) so help output shows both names.
@cli.command(name="ls", short_help="Alias for `releasy list`.")
def ls_cmd() -> None:
    """Alias for `releasy list`."""
    list_cmd.callback()  # type: ignore[misc]


@cli.command(short_help="Print the state file path for the resolved config.")
@click.pass_context
def where(ctx: click.Context) -> None:
    """Print the absolute path of this project's state file."""
    config = _load_config_or_exit(ctx.obj["config_path"])
    click.echo(str(config.state_path))


@cli.command(short_help="Rebind state to the current config (use after moving config).")
@click.pass_context
def adopt(ctx: click.Context) -> None:
    """Rewrite the state file's stored config_path to the current config.

    Use after moving / renaming your config.yaml so subsequent commands
    don't trip the ownership-collision check. Creates an empty state
    file if none exists yet (so `releasy adopt` doubles as "register
    this config without doing anything").
    """
    from releasy.locks import project_lock
    from releasy.state import adopt_ownership

    config = _load_config_or_exit(ctx.obj["config_path"])
    with project_lock(config):
        previous, state_path = adopt_ownership(config)
    if previous is None:
        click.echo(
            f"Project {config.name!r} is now bound to "
            f"{config.config_path}.\nState file: {state_path}"
        )
    else:
        click.echo(
            f"Rebound project {config.name!r} from {previous} to "
            f"{config.config_path}.\nState file: {state_path}"
        )


# ---------------------------------------------------------------------------
# Feature management
# ---------------------------------------------------------------------------


@cli.group()
def feature() -> None:
    """Manage the static `features:` list in the session file."""


@feature.command(name="add")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.option("--source-branch", required=True, help="Existing branch with feature commits")
@click.option("--description", required=True, help="Feature description")
@click.pass_context
def feature_add(
    ctx: click.Context, feature_id: str, source_branch: str, description: str,
) -> None:
    """Add a new feature branch."""
    from releasy.feature import add_feature

    with _locked_config(ctx, session="required") as config:
        if not add_feature(config, feature_id, source_branch, description):
            raise SystemExit(1)


@feature.command(name="enable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_enable(ctx: click.Context, feature_id: str) -> None:
    """Enable a feature branch."""
    from releasy.feature import enable_feature

    with _locked_config(ctx, session="required") as config:
        if not enable_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="disable")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_disable(ctx: click.Context, feature_id: str) -> None:
    """Disable a feature branch."""
    from releasy.feature import disable_feature

    with _locked_config(ctx, session="required") as config:
        if not disable_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="remove")
@click.option("--id", "feature_id", required=True, help="Feature identifier")
@click.pass_context
def feature_remove(ctx: click.Context, feature_id: str) -> None:
    """Remove a feature branch."""
    from releasy.feature import remove_feature

    with _locked_config(ctx, session="required") as config:
        if not remove_feature(config, feature_id):
            raise SystemExit(1)


@feature.command(name="list")
@click.pass_context
def feature_list(ctx: click.Context) -> None:
    """List all configured features."""
    from releasy.feature import list_features

    config = _load_and_verify(ctx, session="required")
    list_features(config)



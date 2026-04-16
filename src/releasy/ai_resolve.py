"""Claude-driven autonomous conflict resolution.

Renders a prompt template, spawns ``claude -p``, streams its output to the
console, enforces a timeout, and verifies the post-conditions (branch pushed,
PR opened, AI label attached) using the existing GitHub helpers.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from releasy.config import Config
from releasy.git_ops import (
    get_branch_tip,
    is_operation_in_progress,
    run_git,
)
from releasy.github_ops import (
    PRInfo,
    add_label_to_pr,
    find_pr_for_branch,
)

console = Console()


@dataclass
class AIResolveContext:
    port_branch: str
    base_branch: str
    source_pr: PRInfo
    conflict_files: list[str] = field(default_factory=list)


@dataclass
class AIResolveResult:
    success: bool
    iterations: int | None = None
    pr_url: str | None = None
    error: str | None = None
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render_prompt(config: Config, repo_path: Path, ctx: AIResolveContext) -> str:
    """Load the prompt template and fill in placeholders."""
    prompt_path = Path(config.ai_resolve.prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = (config.repo_dir / prompt_path).resolve()

    if not prompt_path.exists():
        raise FileNotFoundError(
            f"AI prompt template not found: {prompt_path}. "
            f"Set ai_resolve.prompt_file in config."
        )

    template = prompt_path.read_text(encoding="utf-8")

    from releasy.github_ops import get_origin_repo_slug
    repo_slug = get_origin_repo_slug(config) or "<unknown>"

    conflict_files_md = "\n".join(f"- `{f}`" for f in ctx.conflict_files) or "- (none)"

    body = (ctx.source_pr.body or "").strip()
    if not body:
        body = "_(empty)_"
    elif len(body) > 4000:
        body = body[:4000] + "\n\n_(truncated)_"

    placeholders = {
        "repo_slug": repo_slug,
        "cwd": str(repo_path),
        "port_branch": ctx.port_branch,
        "base_branch": ctx.base_branch,
        "source_pr_url": ctx.source_pr.url,
        "source_pr_title": ctx.source_pr.title,
        "source_pr_number": str(ctx.source_pr.number),
        "source_pr_body": body,
        "conflict_files": conflict_files_md,
        "build_command": config.ai_resolve.build_command,
        "max_iterations": str(config.ai_resolve.max_iterations),
        "label": config.ai_resolve.label,
    }

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return placeholders.get(key, match.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def _build_claude_argv(config: Config, prompt: str) -> list[str]:
    cmd = [config.ai_resolve.command, "-p", prompt]
    if config.ai_resolve.allowed_tools:
        cmd += ["--allowedTools", ",".join(config.ai_resolve.allowed_tools)]
    cmd += list(config.ai_resolve.extra_args)
    return cmd


def _spawn_claude(
    argv: list[str], repo_path: Path, timeout: int,
) -> tuple[int, str, bool]:
    """Run claude as a subprocess, streaming stdout/stderr to the console.

    Returns (exit_code, combined_output, timed_out).
    """
    console.print(
        f"    [dim]$ {shlex.join(argv[:2])} <prompt…> "
        f"{shlex.join(argv[3:])}[/dim]"
    )

    env = os.environ.copy()

    proc = subprocess.Popen(
        argv,
        cwd=repo_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    collected: list[str] = []
    start = time.monotonic()
    timed_out = False

    try:
        assert proc.stdout is not None
        while True:
            if (time.monotonic() - start) > timeout:
                proc.kill()
                timed_out = True
                break
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            collected.append(line)
            console.print(f"    [dim]│[/dim] {line.rstrip()}")
        proc.wait(timeout=30)
    except Exception as exc:
        proc.kill()
        collected.append(f"[runner] error streaming claude output: {exc}\n")

    exit_code = proc.returncode if proc.returncode is not None else -1
    return exit_code, "".join(collected), timed_out


# ---------------------------------------------------------------------------
# Post-condition verification
# ---------------------------------------------------------------------------


def _count_iterations(output: str) -> int | None:
    """Best-effort: count how many build attempts Claude ran.

    Looks for patterns in its own narration. Returns None when unknown.
    """
    patterns = [
        r"build attempt[s]?:?\s*(\d+)",
        r"iteration\s*(\d+)\s*/\s*\d+",
    ]
    best: int | None = None
    for pat in patterns:
        for m in re.finditer(pat, output, re.IGNORECASE):
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            best = n if best is None else max(best, n)
    return best


def _verify_postconditions(
    config: Config, repo_path: Path, ctx: AIResolveContext,
) -> tuple[bool, str | None, str | None]:
    """Check: no op in progress, branch pushed, PR exists with AI label.

    Returns ``(ok, pr_url, error_message)``.
    """
    if is_operation_in_progress(repo_path):
        return False, None, "cherry-pick/merge/rebase still in progress after claude exited"

    result = run_git(
        ["rev-parse", "--verify", ctx.port_branch],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        return False, None, f"local branch {ctx.port_branch} is missing"
    local_sha = result.stdout.strip()

    remote_ref = f"{config.origin.remote_name}/{ctx.port_branch}"
    result = run_git(["rev-parse", "--verify", remote_ref], repo_path, check=False)
    if result.returncode != 0:
        return False, None, f"branch {ctx.port_branch} was not pushed to origin"
    remote_sha = result.stdout.strip()
    if local_sha != remote_sha:
        run_git(["fetch", config.origin.remote_name, ctx.port_branch], repo_path, check=False)
        remote_sha = get_branch_tip(repo_path, remote_ref)
        if local_sha != remote_sha:
            return False, None, (
                f"local and remote {ctx.port_branch} diverge "
                f"({local_sha[:8]} vs {remote_sha[:8]})"
            )

    pr = find_pr_for_branch(config, ctx.port_branch, ctx.base_branch)
    if pr is None:
        return False, None, f"no open PR found for {ctx.port_branch} \u2192 {ctx.base_branch}"

    if config.ai_resolve.label not in pr.labels:
        labelled = add_label_to_pr(config, pr.number, config.ai_resolve.label)
        if not labelled:
            return True, pr.url, f"PR opened but '{config.ai_resolve.label}' label could not be added"

    return True, pr.url, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def resolve_with_claude(
    config: Config, repo_path: Path, ctx: AIResolveContext,
) -> AIResolveResult:
    """Render the prompt, run claude, and verify post-conditions."""
    if shutil.which(config.ai_resolve.command) is None:
        return AIResolveResult(
            success=False,
            error=f"'{config.ai_resolve.command}' not found on PATH",
        )

    try:
        prompt = _render_prompt(config, repo_path, ctx)
    except FileNotFoundError as exc:
        return AIResolveResult(success=False, error=str(exc))

    argv = _build_claude_argv(config, prompt)

    console.print(
        f"    [magenta]\U0001f916 invoking {config.ai_resolve.command} "
        f"(timeout {config.ai_resolve.timeout_seconds}s, "
        f"max {config.ai_resolve.max_iterations} build attempts)[/magenta]"
    )

    exit_code, output, timed_out = _spawn_claude(
        argv, repo_path, config.ai_resolve.timeout_seconds,
    )

    iterations = _count_iterations(output)

    if timed_out:
        return AIResolveResult(
            success=False, timed_out=True, iterations=iterations,
            error=f"claude timed out after {config.ai_resolve.timeout_seconds}s",
        )

    tail = output.strip().splitlines()[-20:] if output.strip() else []
    tail_str = "\n".join(tail)
    if any(line.strip() == "UNRESOLVED" for line in tail):
        return AIResolveResult(
            success=False, iterations=iterations,
            error="claude reported UNRESOLVED",
        )
    if any(line.strip() == "BUILD FAILED" for line in tail):
        return AIResolveResult(
            success=False, iterations=iterations,
            error="claude reported BUILD FAILED",
        )

    if exit_code != 0:
        return AIResolveResult(
            success=False, iterations=iterations,
            error=f"claude exited with code {exit_code}\n{tail_str}",
        )

    ok, pr_url, err = _verify_postconditions(config, repo_path, ctx)
    if not ok:
        return AIResolveResult(
            success=False, iterations=iterations, pr_url=pr_url, error=err,
        )

    return AIResolveResult(
        success=True, iterations=iterations, pr_url=pr_url, error=err,
    )

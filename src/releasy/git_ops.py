"""Git operations via subprocess.

Uses subprocess for full control over cherry-pick/conflict detection.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from releasy.config import Config, get_ssh_key_path


@dataclass
class OperationResult:
    success: bool
    conflict_files: list[str]
    error_message: str | None = None


def _git_env() -> dict[str, str]:
    """Build env dict with SSH key configuration if set."""
    env = os.environ.copy()
    ssh_key = get_ssh_key_path()
    if ssh_key:
        env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=no"
    return env


def run_git(
    args: list[str],
    work_dir: Path,
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given working directory."""
    cmd = ["git"] + args
    return subprocess.run(
        cmd,
        cwd=work_dir,
        env=_git_env(),
        capture_output=capture,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# Repository setup
# ---------------------------------------------------------------------------


def ensure_work_repo(config: Config, work_dir: Path) -> Path:
    """Ensure a local clone of the fork exists at work_dir.

    Sets up both the fork (origin) and upstream remotes.
    Returns the repo path.
    """
    repo_path = work_dir / "repo"

    if not (repo_path / ".git").exists():
        run_git(["clone", config.fork.remote, "repo"], work_dir)
        run_git(
            ["remote", "add", config.upstream.remote_name, config.upstream.remote],
            repo_path,
            check=False,
        )
    else:
        for rname, rurl in [
            (config.fork.remote_name, config.fork.remote),
            (config.upstream.remote_name, config.upstream.remote),
        ]:
            result = run_git(["remote", "get-url", rname], repo_path, check=False)
            if result.returncode != 0:
                run_git(["remote", "add", rname, rurl], repo_path, check=False)

    return repo_path


# ---------------------------------------------------------------------------
# Fetch / checkout
# ---------------------------------------------------------------------------


def fetch_remote(repo_path: Path, remote_name: str) -> None:
    run_git(["fetch", remote_name], repo_path)


def fetch_all(config: Config, repo_path: Path) -> None:
    fetch_remote(repo_path, config.upstream.remote_name)
    fetch_remote(repo_path, config.fork.remote_name)


def stash_and_clean(repo_path: Path) -> None:
    """Ensure the working tree is clean."""
    run_git(["checkout", "--force", "HEAD"], repo_path, check=False)
    run_git(["clean", "-fd"], repo_path, check=False)


# ---------------------------------------------------------------------------
# Branch operations
# ---------------------------------------------------------------------------


def create_branch_from_ref(repo_path: Path, branch_name: str, ref: str) -> None:
    """Create a new branch from a given ref and check it out."""
    run_git(["checkout", "-b", branch_name, ref], repo_path)


def force_push(repo_path: Path, branch: str, remote: str) -> None:
    run_git(["push", "--force", remote, branch], repo_path)


def get_branch_tip(repo_path: Path, ref: str) -> str:
    """Get the SHA of a ref (branch, tag, HEAD, etc.)."""
    result = run_git(["rev-parse", ref], repo_path)
    return result.stdout.strip()


def get_short_sha(repo_path: Path, ref: str) -> str:
    """Get the short (8-char) SHA of a ref."""
    result = run_git(["rev-parse", "--short=8", ref], repo_path)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Merge-base & commit range
# ---------------------------------------------------------------------------


def find_merge_base(repo_path: Path, ref_a: str, ref_b: str) -> str | None:
    """Find the best common ancestor of two refs."""
    result = run_git(["merge-base", ref_a, ref_b], repo_path, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_commit_range(repo_path: Path, base_ref: str, tip_ref: str) -> list[str]:
    """Return list of commit SHAs in (base_ref..tip_ref]."""
    result = run_git(
        ["rev-list", "--reverse", f"{base_ref}..{tip_ref}"],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().splitlines() if s]


def count_commits(repo_path: Path, base_ref: str, tip_ref: str) -> int:
    result = run_git(
        ["rev-list", "--count", f"{base_ref}..{tip_ref}"],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip())


# ---------------------------------------------------------------------------
# Cherry-pick
# ---------------------------------------------------------------------------


def get_conflict_files(repo_path: Path) -> list[str]:
    """Extract conflicting file paths from a failed operation."""
    result = run_git(["diff", "--name-only", "--diff-filter=U"], repo_path, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()

    result = run_git(["status", "--porcelain"], repo_path, check=False)
    conflicts = []
    for line in result.stdout.splitlines():
        if line[:2] in ("UU", "AA", "DD"):
            conflicts.append(line[3:].strip())
    return conflicts


def cherry_pick_range(repo_path: Path, base_ref: str, tip_ref: str) -> OperationResult:
    """Cherry-pick a range of commits (base_ref..tip_ref] onto current branch."""
    commits = get_commit_range(repo_path, base_ref, tip_ref)
    if not commits:
        return OperationResult(success=True, conflict_files=[])

    result = run_git(
        ["cherry-pick", f"{base_ref}..{tip_ref}"],
        repo_path,
        check=False,
    )
    if result.returncode == 0:
        return OperationResult(success=True, conflict_files=[])

    conflict_files = get_conflict_files(repo_path)
    run_git(["cherry-pick", "--abort"], repo_path, check=False)
    return OperationResult(
        success=False,
        conflict_files=conflict_files,
        error_message=result.stderr.strip() if result.stderr else None,
    )


# ---------------------------------------------------------------------------
# Squash
# ---------------------------------------------------------------------------


def squash_commits(repo_path: Path, base_ref: str, message: str) -> OperationResult:
    """Squash all commits since base_ref into a single commit."""
    mb = find_merge_base(repo_path, "HEAD", base_ref)
    if mb is None:
        return OperationResult(
            success=False, conflict_files=[],
            error_message=f"Could not find merge-base between HEAD and {base_ref}",
        )

    if count_commits(repo_path, mb, "HEAD") == 0:
        return OperationResult(success=True, conflict_files=[])

    run_git(["reset", "--soft", mb], repo_path)
    result = run_git(["commit", "-m", message], repo_path, check=False)

    if result.returncode != 0:
        return OperationResult(
            success=False, conflict_files=[],
            error_message=result.stderr.strip() if result.stderr else None,
        )
    return OperationResult(success=True, conflict_files=[])


# ---------------------------------------------------------------------------
# Ref resolution helpers
# ---------------------------------------------------------------------------


def resolve_ref(repo_path: Path, ref: str) -> str | None:
    """Try to resolve a ref (tag, branch, sha). Returns full SHA or None."""
    for candidate in [ref, f"refs/tags/{ref}"]:
        result = run_git(["rev-parse", candidate], repo_path, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    return None

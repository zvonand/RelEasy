"""Git operations: clone, fetch, rebase, push via subprocess.

Uses subprocess rather than GitPython's higher-level API for rebase operations,
since GitPython doesn't natively support rebase with conflict detection.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from releasy.config import Config, get_ssh_key_path


@dataclass
class RebaseResult:
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


def _run_git(
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


def ensure_work_repo(config: Config, work_dir: Path) -> Path:
    """Ensure a local clone of the fork exists at work_dir.

    Sets up both the fork (origin) and upstream remotes.
    Returns the repo path.
    """
    repo_path = work_dir / "repo"

    if not (repo_path / ".git").exists():
        _run_git(["clone", config.fork.remote, "repo"], work_dir)
        _run_git(
            ["remote", "add", config.upstream.remote_name, config.upstream.remote],
            repo_path,
            check=False,
        )
    else:
        # Ensure remotes are up to date
        result = _run_git(
            ["remote", "get-url", config.fork.remote_name], repo_path, check=False
        )
        if result.returncode != 0:
            _run_git(
                ["remote", "add", config.fork.remote_name, config.fork.remote],
                repo_path,
                check=False,
            )

        result = _run_git(
            ["remote", "get-url", config.upstream.remote_name], repo_path, check=False
        )
        if result.returncode != 0:
            _run_git(
                ["remote", "add", config.upstream.remote_name, config.upstream.remote],
                repo_path,
                check=False,
            )

    return repo_path


def fetch_remote(repo_path: Path, remote_name: str) -> None:
    """Fetch all refs from a remote."""
    _run_git(["fetch", remote_name], repo_path)


def fetch_all(config: Config, repo_path: Path) -> None:
    """Fetch from both upstream and fork remotes."""
    fetch_remote(repo_path, config.upstream.remote_name)
    fetch_remote(repo_path, config.fork.remote_name)


def checkout_branch(repo_path: Path, branch: str, remote: str | None = None) -> None:
    """Check out a branch locally, creating a tracking branch if needed."""
    # Try simple checkout first
    result = _run_git(["checkout", branch], repo_path, check=False)
    if result.returncode != 0 and remote:
        # Create local tracking branch from remote
        _run_git(
            ["checkout", "-b", branch, f"{remote}/{branch}"],
            repo_path,
            check=False,
        )
        # If that also fails, try resetting to remote
        result2 = _run_git(["checkout", branch], repo_path, check=False)
        if result2.returncode != 0:
            raise RuntimeError(f"Failed to checkout {branch}: {result.stderr}")

    # Reset to remote tracking if remote specified (ensure we're at remote HEAD)
    if remote:
        _run_git(["reset", "--hard", f"{remote}/{branch}"], repo_path, check=False)


def rebase_onto(repo_path: Path, onto_ref: str) -> RebaseResult:
    """Rebase the current branch onto the given ref.

    Returns a RebaseResult indicating success or conflict with file list.
    """
    result = _run_git(["rebase", onto_ref], repo_path, check=False)

    if result.returncode == 0:
        return RebaseResult(success=True, conflict_files=[])

    # Rebase failed — extract conflicting files
    conflict_files = _get_conflict_files(repo_path)

    # Abort the rebase to leave the branch untouched
    _run_git(["rebase", "--abort"], repo_path, check=False)

    return RebaseResult(
        success=False,
        conflict_files=conflict_files,
        error_message=result.stderr.strip() if result.stderr else None,
    )


def _get_conflict_files(repo_path: Path) -> list[str]:
    """Extract conflicting file paths from a failed rebase."""
    result = _run_git(["diff", "--name-only", "--diff-filter=U"], repo_path, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()

    # Fallback: parse from status
    result = _run_git(["status", "--porcelain"], repo_path, check=False)
    conflicts = []
    for line in result.stdout.splitlines():
        if line.startswith("UU ") or line.startswith("AA ") or line.startswith("DD "):
            conflicts.append(line[3:].strip())
    return conflicts


def force_push(repo_path: Path, branch: str, remote: str) -> None:
    """Force-push a branch to the remote."""
    _run_git(["push", "--force", remote, branch], repo_path)


def create_branch_from_ref(repo_path: Path, branch_name: str, ref: str) -> None:
    """Create a new branch from a given ref (tag, sha, etc.)."""
    _run_git(["checkout", "-b", branch_name, ref], repo_path)


def get_branch_tip(repo_path: Path, branch: str) -> str:
    """Get the SHA of the tip of a branch."""
    result = _run_git(["rev-parse", branch], repo_path)
    return result.stdout.strip()


def is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    """Check if `ancestor` is an ancestor of `descendant`."""
    result = _run_git(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        repo_path,
        check=False,
    )
    return result.returncode == 0


def cherry_pick_range(
    repo_path: Path, base_ref: str, tip_ref: str
) -> RebaseResult:
    """Cherry-pick a range of commits (base_ref..tip_ref] onto current branch."""
    result = _run_git(
        ["cherry-pick", f"{base_ref}..{tip_ref}"],
        repo_path,
        check=False,
    )
    if result.returncode == 0:
        return RebaseResult(success=True, conflict_files=[])

    conflict_files = _get_conflict_files(repo_path)
    _run_git(["cherry-pick", "--abort"], repo_path, check=False)
    return RebaseResult(
        success=False,
        conflict_files=conflict_files,
        error_message=result.stderr.strip() if result.stderr else None,
    )


def squash_commits(repo_path: Path, base_ref: str, message: str) -> RebaseResult:
    """Squash all commits since base_ref into a single commit with the given message.

    Operates on the current branch. Uses reset --soft to squash.
    """
    # Find the merge base
    result = _run_git(["merge-base", "HEAD", base_ref], repo_path, check=False)
    if result.returncode != 0:
        return RebaseResult(
            success=False,
            conflict_files=[],
            error_message=f"Could not find merge-base between HEAD and {base_ref}",
        )
    merge_base = result.stdout.strip()

    # Count commits to squash
    count_result = _run_git(
        ["rev-list", "--count", f"{merge_base}..HEAD"], repo_path, check=False
    )
    if count_result.returncode != 0 or int(count_result.stdout.strip()) == 0:
        return RebaseResult(success=True, conflict_files=[])

    # Soft reset to merge base, then commit everything as one
    _run_git(["reset", "--soft", merge_base], repo_path)
    result = _run_git(["commit", "-m", message], repo_path, check=False)

    if result.returncode != 0:
        return RebaseResult(
            success=False,
            conflict_files=[],
            error_message=result.stderr.strip() if result.stderr else None,
        )

    return RebaseResult(success=True, conflict_files=[])


def get_commit_range(repo_path: Path, base_ref: str, tip_ref: str) -> list[str]:
    """Return list of commit SHAs in (base_ref..tip_ref]."""
    result = _run_git(
        ["rev-list", "--reverse", f"{base_ref}..{tip_ref}"],
        repo_path,
        check=False,
    )
    if result.returncode != 0:
        return []
    return result.stdout.strip().splitlines()


def stash_and_clean(repo_path: Path) -> None:
    """Ensure the working tree is clean."""
    _run_git(["checkout", "--force", "HEAD"], repo_path, check=False)
    _run_git(["clean", "-fd"], repo_path, check=False)

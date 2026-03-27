"""GitHub operations: commit/push state, project board sync, PR creation, and PR search."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from releasy.config import Config, get_github_token
from releasy.state import PipelineState

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# Remote URL parsing
# ---------------------------------------------------------------------------


def parse_remote_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub remote URL.

    Supports SSH (git@github.com:owner/repo.git) and
    HTTPS (https://github.com/owner/repo.git).
    """
    m = re.match(r"git@github\.com:(.+)/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"https://github\.com/(.+)/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def get_fork_repo_slug(config: Config) -> str | None:
    """Return 'owner/repo' for the fork remote from config."""
    parsed = parse_remote_url(config.fork.remote)
    if not parsed:
        return None
    return f"{parsed[0]}/{parsed[1]}"


# ---------------------------------------------------------------------------
# Git state commit/push
# ---------------------------------------------------------------------------


def commit_and_push_state(message: str, repo_dir: Path | None = None) -> bool:
    """Commit state.yaml and STATUS.md in the tool repo and push."""
    if repo_dir is None:
        repo_dir = Path.cwd()
    files = ["state.yaml", "STATUS.md"]
    existing = [f for f in files if (repo_dir / f).exists()]
    if not existing:
        return False

    try:
        subprocess.run(["git", "add"] + existing, cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_dir,
            check=False,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


# ---------------------------------------------------------------------------
# Pull Request creation (PyGithub REST API)
# ---------------------------------------------------------------------------


def _assert_not_upstream(config: Config, slug: str) -> None:
    """Hard safeguard: refuse to operate against the upstream repo."""
    upstream_parsed = parse_remote_url(config.upstream.remote)
    if upstream_parsed:
        upstream_slug = f"{upstream_parsed[0]}/{upstream_parsed[1]}"
        if slug == upstream_slug:
            raise ValueError(
                f"CRITICAL: Refusing to create PR against upstream repo '{slug}'. "
                "PRs must only target the fork."
            )


def create_pull_request(
    config: Config,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str | None:
    """Create a pull request on the fork repo.

    Args:
        config: Config with fork remote URL.
        head: Source branch name.
        base: Target branch name.
        title: PR title.
        body: PR body (markdown).

    Returns the PR URL, or None if creation failed.
    Raises ValueError if the target repo matches upstream.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot create PR")
        return None

    slug = get_fork_repo_slug(config)
    if not slug:
        log.warning("Could not parse fork remote URL: %s", config.fork.remote)
        return None

    _assert_not_upstream(config, slug)

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        pr = repo.create_pull(title=title, body=body, head=head, base=base)
        return pr.html_url
    except GithubException as exc:
        log.warning("Failed to create PR: %s", exc)
        return None
    except Exception as exc:
        log.warning("Unexpected error creating PR: %s", exc)
        return None


# ---------------------------------------------------------------------------
# PR search by label
# ---------------------------------------------------------------------------


@dataclass
class PRInfo:
    number: int
    title: str
    body: str
    state: str  # "open" or "merged"
    merge_commit_sha: str | None
    head_sha: str
    url: str
    merged_at: str | None = None  # ISO timestamp of merge


def search_prs_by_labels(
    config: Config,
    labels: list[str],
    merged_only: bool = False,
) -> list[PRInfo]:
    """Search the fork repo for PRs that have ALL specified labels.

    Returns PRs sorted by merge date (earliest first), with open PRs last.
    Skips closed-but-not-merged PRs.
    When merged_only is True, only merged PRs are returned.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — cannot search PRs")
        return []

    slug = get_fork_repo_slug(config)
    if not slug:
        log.warning("Could not parse fork remote URL: %s", config.fork.remote)
        return []

    if not labels:
        return []

    try:
        from github import Github, GithubException

        gh = Github(token)
        repo = gh.get_repo(slug)
        results: list[PRInfo] = []

        # GitHub API filters by all labels when given a list, so this
        # already implements AND semantics.
        for issue in repo.get_issues(labels=labels, state="all"):
            if issue.pull_request is None:
                continue
            pr = repo.get_pull(issue.number)
            if pr.merged:
                pr_state = "merged"
            elif pr.state == "open":
                if merged_only:
                    continue
                pr_state = "open"
            else:
                continue  # closed but not merged — skip

            results.append(PRInfo(
                number=pr.number,
                title=pr.title,
                body=pr.body or "",
                state=pr_state,
                merge_commit_sha=pr.merge_commit_sha if pr.merged else None,
                head_sha=pr.head.sha,
                url=pr.html_url,
                merged_at=pr.merged_at.isoformat() if pr.merged_at else None,
            ))

        # Merged PRs first in merge order, then open PRs by number.
        results.sort(key=lambda p: (p.merged_at or "9999", p.number))
        return results
    except GithubException as exc:
        log.warning("Failed to search PRs by labels %s: %s", labels, exc)
        return []
    except Exception as exc:
        log.warning("Unexpected error searching PRs: %s", exc)
        return []


# ---------------------------------------------------------------------------
# GitHub Projects v2 integration (GraphQL API)
# ---------------------------------------------------------------------------


def _gql(query: str, variables: dict | None = None) -> dict | None:
    """Execute a GitHub GraphQL query."""
    token = get_github_token()
    if not token:
        return None

    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log.warning("GitHub GraphQL request failed: %s %s", resp.status_code, resp.text)
        return None

    data = resp.json()
    if "errors" in data:
        log.warning("GitHub GraphQL errors: %s", data["errors"])
    return data.get("data")


def _parse_project_url(url: str) -> tuple[str, int, bool] | None:
    """Parse a GitHub Project URL into (owner, number, is_org)."""
    m = re.match(r"https://github\.com/orgs/([^/]+)/projects/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2)), True
    m = re.match(r"https://github\.com/users/([^/]+)/projects/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2)), False
    return None


def _get_project_id(owner: str, number: int, is_org: bool) -> str | None:
    if is_org:
        query = """
        query($owner: String!, $number: Int!) {
          organization(login: $owner) {
            projectV2(number: $number) { id }
          }
        }
        """
    else:
        query = """
        query($owner: String!, $number: Int!) {
          user(login: $owner) {
            projectV2(number: $number) { id }
          }
        }
        """
    data = _gql(query, {"owner": owner, "number": number})
    if not data:
        return None
    try:
        key = "organization" if is_org else "user"
        return data[key]["projectV2"]["id"]
    except (KeyError, TypeError):
        return None


def _get_status_field(project_id: str) -> tuple[str, dict[str, str]] | None:
    query = """
    query($projectId: ID!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          fields(first: 30) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id
                name
                options { id name }
              }
            }
          }
        }
      }
    }
    """
    data = _gql(query, {"projectId": project_id})
    if not data:
        return None
    try:
        field_nodes = data["node"]["fields"]["nodes"]
    except (KeyError, TypeError):
        return None

    for field_node in field_nodes:
        if field_node.get("name", "").lower() == "status":
            options = {
                opt["name"].lower(): opt["id"]
                for opt in field_node.get("options", [])
            }
            return field_node["id"], options
    return None


def _find_item_by_title(project_id: str, title: str) -> tuple[str, str | None] | None:
    """Find a project item by its draft issue title.

    Returns (item_id, draft_issue_id) or None.
    """
    query = """
    query($projectId: ID!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100) {
            nodes {
              id
              content {
                ... on DraftIssue { id title }
              }
            }
          }
        }
      }
    }
    """
    data = _gql(query, {"projectId": project_id})
    if not data:
        return None
    try:
        items = data["node"]["items"]["nodes"]
    except (KeyError, TypeError):
        return None

    for item in items:
        content = item.get("content")
        if content and content.get("title") == title:
            return item["id"], content.get("id")
    return None


def _add_draft_issue(project_id: str, title: str, body: str) -> str | None:
    mutation = """
    mutation($projectId: ID!, $title: String!, $body: String!) {
      addProjectV2DraftIssue(input: {projectId: $projectId, title: $title, body: $body}) {
        projectItem { id }
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "title": title, "body": body})
    if not data:
        return None
    try:
        return data["addProjectV2DraftIssue"]["projectItem"]["id"]
    except (KeyError, TypeError):
        return None


def _update_draft_issue(
    project_id: str, item_id: str,
    title: str | None = None, body: str | None = None,
) -> bool:
    """Update an existing draft issue's title and/or body."""
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $title: String, $body: String) {
      updateProjectV2DraftIssue(input: {
        projectId: $projectId, draftIssueId: $itemId,
        title: $title, body: $body
      }) {
        draftIssue { id }
      }
    }
    """
    variables: dict = {"projectId": project_id, "itemId": item_id}
    if title is not None:
        variables["title"] = title
    if body is not None:
        variables["body"] = body
    data = _gql(mutation, variables)
    return data is not None


def _set_item_field(project_id: str, item_id: str, field_id: str, option_id: str) -> bool:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId
        itemId: $itemId
        fieldId: $fieldId
        value: { singleSelectOptionId: $optionId }
      }) {
        projectV2Item { id }
      }
    }
    """
    data = _gql(mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "optionId": option_id,
    })
    return data is not None


def _delete_item(project_id: str, item_id: str) -> bool:
    mutation = """
    mutation($projectId: ID!, $itemId: ID!) {
      deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
        deletedItemId
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "itemId": item_id})
    return data is not None


STATUS_OPTIONS = ["Ok", "Conflict", "Resolved", "Skipped", "Disabled", "Pending"]

STATUS_COLORS = {
    "Ok": "GREEN",
    "Conflict": "RED",
    "Resolved": "BLUE",
    "Skipped": "YELLOW",
    "Disabled": "GRAY",
    "Pending": "ORANGE",
}


def _get_owner_id(owner: str, is_org: bool) -> str | None:
    if is_org:
        query = "query($login: String!) { organization(login: $login) { id } }"
    else:
        query = "query($login: String!) { user(login: $login) { id } }"
    data = _gql(query, {"login": owner})
    if not data:
        return None
    try:
        key = "organization" if is_org else "user"
        return data[key]["id"]
    except (KeyError, TypeError):
        return None


def _create_project(owner_id: str, title: str) -> tuple[str, int] | None:
    """Create a new GitHub Project v2. Returns (project_id, project_number)."""
    mutation = """
    mutation($ownerId: ID!, $title: String!) {
      createProjectV2(input: {ownerId: $ownerId, title: $title}) {
        projectV2 { id number }
      }
    }
    """
    data = _gql(mutation, {"ownerId": owner_id, "title": title})
    if not data:
        return None
    try:
        p = data["createProjectV2"]["projectV2"]
        return p["id"], p["number"]
    except (KeyError, TypeError):
        return None


def _create_single_select_field(
    project_id: str, name: str, options: list[dict],
) -> str | None:
    """Create a single-select field on a project. Returns field ID."""
    mutation = """
    mutation($projectId: ID!, $name: String!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: $name
        singleSelectOptions: $options
      }) {
        projectV2Field { ... on ProjectV2SingleSelectField { id } }
      }
    }
    """
    data = _gql(mutation, {
        "projectId": project_id,
        "name": name,
        "options": options,
    })
    if not data:
        return None
    try:
        return data["createProjectV2Field"]["projectV2Field"]["id"]
    except (KeyError, TypeError):
        return None


def setup_project(config: Config) -> str | None:
    """Create a GitHub Project with the Status field, or verify an existing one.

    If notifications.github_project is set, verifies the Status field exists.
    If not set, creates a new project and returns the URL.
    """
    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set")
        return None

    slug = get_fork_repo_slug(config)
    if not slug:
        return None
    owner = slug.split("/")[0]

    project_url = config.notifications.github_project

    if project_url:
        parsed = _parse_project_url(project_url)
        if not parsed:
            log.warning("Could not parse project URL: %s", project_url)
            return None
        p_owner, p_number, is_org = parsed
        project_id = _get_project_id(p_owner, p_number, is_org)
        if not project_id:
            log.warning("Could not find project: %s", project_url)
            return None
    else:
        is_org = True
        owner_id = _get_owner_id(owner, is_org)
        if not owner_id:
            is_org = False
            owner_id = _get_owner_id(owner, is_org)
        if not owner_id:
            log.warning("Could not resolve owner ID for %s", owner)
            return None

        title = f"RelEasy: {config.project_name}"
        result = _create_project(owner_id, title)
        if not result:
            log.warning("Failed to create project")
            return None
        project_id, p_number = result
        if is_org:
            project_url = f"https://github.com/orgs/{owner}/projects/{p_number}"
        else:
            project_url = f"https://github.com/users/{owner}/projects/{p_number}"

    status_info = _get_status_field(project_id)
    if status_info:
        _, existing_options = status_info
        missing = [
            opt for opt in STATUS_OPTIONS
            if opt.lower() not in existing_options
        ]
        if missing:
            log.warning(
                "Status field exists but missing options: %s. "
                "Please add them manually in the project settings.",
                ", ".join(missing),
            )
    else:
        options = [
            {"name": opt, "color": STATUS_COLORS.get(opt, "GRAY")}
            for opt in STATUS_OPTIONS
        ]
        field_id = _create_single_select_field(project_id, "Status", options)
        if not field_id:
            log.warning("Failed to create Status field")

    return project_url


STATUS_MAP = {
    "ok": "Ok",
    "conflict": "Conflict",
    "resolved": "Resolved",
    "skipped": "Skipped",
    "disabled": "Disabled",
    "pending": "Pending",
}

REST_API_URL = "https://api.github.com"


def _rest_api(
    method: str, path: str, json_data: dict | None = None,
) -> dict | None:
    """Make a GitHub REST API call."""
    token = get_github_token()
    if not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.request(
        method, f"{REST_API_URL}{path}",
        json=json_data, headers=headers, timeout=30,
    )
    if resp.status_code not in (200, 201):
        log.warning("REST API %s %s: %s %s", method, path, resp.status_code, resp.text)
        return None
    return resp.json() if resp.text else {}


def _get_project_views(owner: str, project_number: int, is_org: bool) -> list[dict]:
    """List existing views on a project."""
    prefix = "orgs" if is_org else "users"
    data = _rest_api("GET", f"/{prefix}/{owner}/projects/{project_number}/views")
    if isinstance(data, list):
        return data
    return []


def _create_project_view(
    owner: str, project_number: int, is_org: bool,
    name: str, layout: str = "table",
) -> dict | None:
    """Create a new view (tab) on a project."""
    prefix = "orgs" if is_org else "users"
    return _rest_api(
        "POST",
        f"/{prefix}/{owner}/projects/{project_number}/views",
        {"name": name, "layout": layout},
    )


def _ensure_project_view(
    owner: str, project_number: int, is_org: bool, view_name: str,
) -> bool:
    """Create a view for this rebase if it doesn't exist yet."""
    views = _get_project_views(owner, project_number, is_org)
    for v in views:
        if v.get("name") == view_name:
            return True
    result = _create_project_view(owner, project_number, is_org, view_name)
    return result is not None


def sync_project(config: Config, state: PipelineState) -> bool:
    """Sync pipeline state to a GitHub Project board."""
    project_url = config.notifications.github_project
    if not project_url:
        return False

    token = get_github_token()
    if not token:
        log.warning("RELEASY_GITHUB_TOKEN not set — skipping project sync")
        return False

    parsed = _parse_project_url(project_url)
    if not parsed:
        log.warning("Could not parse project URL: %s", project_url)
        return False

    owner, number, is_org = parsed
    project_id = _get_project_id(owner, number, is_org)
    if not project_id:
        log.warning("Could not resolve project ID for %s", project_url)
        return False

    if state.base_branch:
        _ensure_project_view(owner, number, is_org, state.base_branch)

    status_info = _get_status_field(project_id)
    status_field_id = None
    status_options: dict[str, str] = {}
    if status_info:
        status_field_id, status_options = status_info

    branches: list[tuple[str, str, list[str]]] = []

    ci_label = state.ci_branch.branch_name or config.ci.branch_prefix
    branches.append((
        ci_label,
        state.ci_branch.status,
        state.ci_branch.conflict_files,
    ))

    for feat in config.features:
        fs = state.features.get(feat.id)
        if fs:
            label = fs.branch_name or feat.source_branch
            branches.append((f"{label} ({feat.id})", fs.status, fs.conflict_files))
        else:
            status = "disabled" if not feat.enabled else "pending"
            branches.append((f"{feat.source_branch} ({feat.id})", status, []))

    synced = 0
    for title, status, conflict_files in branches:
        body_parts = [f"**Status:** {status}"]
        if state.onto:
            body_parts.append(f"**Onto:** `{state.onto}`")
        if state.base_branch:
            body_parts.append(f"**Base:** `{state.base_branch}`")
        if conflict_files:
            files_str = "\n".join(f"- `{f}`" for f in conflict_files)
            body_parts.append(f"**Conflict files:**\n{files_str}")
        body = "\n\n".join(body_parts)

        existing = _find_item_by_title(project_id, title)
        if existing:
            item_id, draft_id = existing
            if draft_id:
                _update_draft_issue(project_id, draft_id, body=body)
        else:
            item_id = _add_draft_issue(project_id, title, body)
            if not item_id:
                log.warning("Failed to create project item for %s", title)
                continue

        if status_field_id and status_options:
            mapped = STATUS_MAP.get(status, "Pending")
            option_id = status_options.get(mapped.lower())
            if option_id:
                _set_item_field(project_id, item_id, status_field_id, option_id)

        synced += 1

    log.info("Synced %d items to GitHub Project", synced)
    return synced > 0

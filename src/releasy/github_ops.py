"""GitHub operations: commit/push state files and sync to GitHub Projects v2."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import requests

from releasy.config import Config, get_github_token
from releasy.state import PipelineState

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"


# ---------------------------------------------------------------------------
# Git state commit/push
# ---------------------------------------------------------------------------


def commit_and_push_state(message: str) -> bool:
    """Commit state.yaml and STATUS.md in the tool repo and push."""
    cwd = Path.cwd()
    files = ["state.yaml", "STATUS.md"]
    existing = [f for f in files if (cwd / f).exists()]
    if not existing:
        return False

    try:
        subprocess.run(["git", "add"] + existing, cwd=cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


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
    """Parse a GitHub Project URL into (owner, number, is_org).

    Supports:
      https://github.com/orgs/<org>/projects/<n>
      https://github.com/users/<user>/projects/<n>
    """
    m = re.match(r"https://github\.com/orgs/([^/]+)/projects/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2)), True
    m = re.match(r"https://github\.com/users/([^/]+)/projects/(\d+)", url)
    if m:
        return m.group(1), int(m.group(2)), False
    return None


def _get_project_id(owner: str, number: int, is_org: bool) -> str | None:
    """Resolve a project's node ID from owner + number."""
    if is_org:
        query = """
        query($owner: String!, $number: Int!) {
          organization(login: $owner) {
            projectV2(number: $number) { id }
          }
        }
        """
        data = _gql(query, {"owner": owner, "number": number})
        return data["organization"]["projectV2"]["id"] if data else None
    else:
        query = """
        query($owner: String!, $number: Int!) {
          user(login: $owner) {
            projectV2(number: $number) { id }
          }
        }
        """
        data = _gql(query, {"owner": owner, "number": number})
        return data["user"]["projectV2"]["id"] if data else None


def _get_status_field(project_id: str) -> tuple[str, dict[str, str]] | None:
    """Find the Status single-select field and its option IDs.

    Returns (field_id, {option_name_lower: option_id}).
    """
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

    for field_node in data["node"]["fields"]["nodes"]:
        if field_node.get("name", "").lower() == "status":
            options = {
                opt["name"].lower(): opt["id"]
                for opt in field_node.get("options", [])
            }
            return field_node["id"], options
    return None


def _find_item_by_title(project_id: str, title: str) -> str | None:
    """Find an existing draft issue in the project by title."""
    query = """
    query($projectId: ID!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100) {
            nodes {
              id
              content {
                ... on DraftIssue { title }
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

    for item in data["node"]["items"]["nodes"]:
        content = item.get("content")
        if content and content.get("title") == title:
            return item["id"]
    return None


def _add_draft_issue(project_id: str, title: str, body: str) -> str | None:
    """Add a draft issue to the project. Returns the item ID."""
    mutation = """
    mutation($projectId: ID!, $title: String!, $body: String!) {
      addProjectV2DraftIssue(input: {projectId: $projectId, title: $title, body: $body}) {
        projectItem { id }
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "title": title, "body": body})
    if data:
        return data["addProjectV2DraftIssue"]["projectItem"]["id"]
    return None


def _update_draft_issue_body(item_id: str, body: str) -> bool:
    """Update the body of an existing draft issue."""
    mutation = """
    mutation($itemId: ID!, $body: String!) {
      updateProjectV2DraftIssue(input: {draftIssueId: $itemId, body: $body}) {
        draftIssue { id }
      }
    }
    """
    # The API actually expects the DraftIssue ID, not the item ID.
    # We need the content ID. For simplicity, we recreate items instead.
    # This mutation uses the draft issue's own ID, which we don't always have.
    # Fall through to upsert pattern in sync_project.
    return False


def _set_item_field(project_id: str, item_id: str, field_id: str, option_id: str) -> bool:
    """Set a single-select field value on a project item."""
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
    """Remove an item from the project."""
    mutation = """
    mutation($projectId: ID!, $itemId: ID!) {
      deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
        deletedItemId
      }
    }
    """
    data = _gql(mutation, {"projectId": project_id, "itemId": item_id})
    return data is not None


STATUS_MAP = {
    "ok": "Ok",
    "conflict": "Conflict",
    "resolved": "Resolved",
    "skipped": "Skipped",
    "disabled": "Disabled",
    "pending": "Pending",
}


def sync_project(config: Config, state: PipelineState) -> bool:
    """Sync pipeline state to a GitHub Project board.

    Creates/updates one draft-issue card per branch with its current status.
    Expects the project to have a single-select "Status" field with options
    matching: Ok, Conflict, Resolved, Skipped, Disabled, Pending.

    Returns True if sync succeeded, False if skipped or failed.
    """
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

    # Get the Status field
    status_info = _get_status_field(project_id)
    status_field_id = None
    status_options: dict[str, str] = {}
    if status_info:
        status_field_id, status_options = status_info

    # Build the list of branches to sync
    branches: list[tuple[str, str, list[str]]] = []  # (title, status, conflict_files)

    # CI branch
    branches.append((
        config.ci_branch,
        state.ci_branch.status,
        state.ci_branch.conflict_files,
    ))

    # Feature branches
    for feat in config.features:
        fs = state.features.get(feat.id)
        if fs:
            branches.append((f"{feat.branch} ({feat.id})", fs.status, fs.conflict_files))
        else:
            status = "disabled" if not feat.enabled else "pending"
            branches.append((f"{feat.branch} ({feat.id})", status, []))

    synced = 0
    for title, status, conflict_files in branches:
        body_parts = [f"**Status:** {status}"]
        if state.onto:
            body_parts.append(f"**Onto:** `{state.onto}`")
        if conflict_files:
            files_str = "\n".join(f"- `{f}`" for f in conflict_files)
            body_parts.append(f"**Conflict files:**\n{files_str}")
        body = "\n\n".join(body_parts)

        # Upsert: find existing item or create new one
        item_id = _find_item_by_title(project_id, title)
        if item_id:
            # Delete and recreate to update the body
            _delete_item(project_id, item_id)

        item_id = _add_draft_issue(project_id, title, body)
        if not item_id:
            log.warning("Failed to create project item for %s", title)
            continue

        # Set the Status field if available
        if status_field_id and status_options:
            mapped = STATUS_MAP.get(status, "Pending")
            option_id = status_options.get(mapped.lower())
            if option_id:
                _set_item_field(project_id, item_id, status_field_id, option_id)

        synced += 1

    log.info("Synced %d items to GitHub Project", synced)
    return synced > 0

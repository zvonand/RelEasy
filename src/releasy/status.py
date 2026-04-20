"""Shared display constants for pipeline status output.

Kept as a tiny module of its own so ``pipeline.print_status`` and any
future status renderer (`releasy list`, project-board sync, …) can share
the same icon / heading vocabulary without depending on the old
STATUS.md generator (which was removed when state moved out of the
user's repo).
"""

from __future__ import annotations


STATUS_ICONS: dict[str, str] = {
    "needs_review": "\U0001f535 needs-review",
    "branch_created": "\U0001f7e1 branch-created",
    "conflict": "\U0001f534 conflict",
    "skipped": "\u23ed skipped",
    "merged": "\u2705 merged",
}

STATUS_HEADINGS: dict[str, str] = {
    "needs_review": "Needs Review",
    "branch_created": "Branch Created \u2014 PR not opened yet",
    "conflict": "Conflict \u2014 unresolved (manual fix required)",
    "skipped": "Skipped",
    "merged": "Merged \u2014 landed on target branch",
}

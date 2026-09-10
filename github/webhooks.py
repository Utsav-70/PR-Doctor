"""Webhook signature verification and event parsing.

This module is the security boundary of the whole system: anything that gets past
`verify_signature` is treated as coming from GitHub.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Literal

HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

SkipReason = Literal["draft", "bot_author", "unhandled_action", "unhandled_event"]


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify GitHub's `X-Hub-Signature-256` header against the raw request body.

    Two things here are not stylistic:

    - `raw_body` must be the bytes as received. Re-serialising a parsed dict changes
      key order and whitespace, and the MAC will not match.
    - The comparison uses `hmac.compare_digest`, not `==`. A short-circuiting compare
      leaks the correct prefix through timing.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    """The fields needed to create a review. No API calls involved."""

    installation_id: int
    repository_id: int
    repository_full_name: str
    default_branch: str
    pr_number: int
    head_sha: str
    base_sha: str
    action: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class SkippedEvent:
    reason: SkipReason
    detail: str = ""


def parse_pull_request_event(
    event_name: str,
    payload: dict[str, Any],
    *,
    skip_drafts: bool = True,
    skip_bots: bool = True,
) -> PullRequestEvent | SkippedEvent:
    """Extract review inputs from a `pull_request` payload, or say why we skipped it.

    Returning a reason rather than None keeps the endpoint's logging useful — "why did
    nothing happen for PR #7" is a question you will ask.
    """
    if event_name != "pull_request":
        return SkippedEvent("unhandled_event", event_name)

    action = str(payload.get("action", ""))
    if action not in HANDLED_ACTIONS:
        return SkippedEvent("unhandled_action", action)

    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    installation = payload.get("installation") or {}

    if skip_drafts and pr.get("draft"):
        return SkippedEvent("draft")
    if skip_bots and (pr.get("user") or {}).get("type") == "Bot":
        return SkippedEvent("bot_author", str((pr.get("user") or {}).get("login", "")))

    # `head.sha` rather than the top-level `after`: head.sha is the commit that will
    # actually be reviewed, and the two can disagree on force-push.
    return PullRequestEvent(
        installation_id=int(installation["id"]),
        repository_id=int(repo["id"]),
        repository_full_name=str(repo["full_name"]),
        default_branch=str(repo.get("default_branch", "main")),
        pr_number=int(pr["number"]),
        head_sha=str(pr["head"]["sha"]),
        base_sha=str(pr["base"]["sha"]),
        action=action,
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
    )

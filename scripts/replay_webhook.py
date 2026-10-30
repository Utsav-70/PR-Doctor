"""Replay a real PR as a signed webhook delivery, without a tunnel.

Fetches a real pull request from GitHub with the App's installation token, wraps it in
the `pull_request` payload shape GitHub sends, signs it with the real webhook secret,
and POSTs it to a locally running API.

This exercises the true signature path, the dedupe upsert, and the Celery handoff. The
only thing it does not exercise is GitHub's own delivery — everything downstream of the
HTTP request is identical to production.

    python3 scripts/replay_webhook.py pallets/flask 5918
    python3 scripts/replay_webhook.py Utsav-70/PR-Doctor 1 --action synchronize
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import pathlib
import sys
from typing import Any

import httpx

# Running this as a file puts sys.path[0] at scripts/, which hides the project root.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from github.app_auth import build_app_jwt, get_installation_token
from settings import get_settings


async def _installation_id() -> int:
    """Discover the App's single installation. Avoids hardcoding the ID."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.GITHUB_TIMEOUT_SECONDS) as http:
        response = await http.get(
            f"{settings.GITHUB_API_URL}/app/installations",
            headers={
                "Authorization": f"Bearer {build_app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    response.raise_for_status()
    installations = response.json()
    if not installations:
        sys.exit("No installations found. Install the App on an account first.")
    if len(installations) > 1:
        print(f"note: {len(installations)} installations; using the first")
    return int(installations[0]["id"])


async def _fetch(
    owner: str, repo: str, number: int, installation_id: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = get_settings()
    token = await get_installation_token(installation_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(
        base_url=settings.GITHUB_API_URL, timeout=settings.GITHUB_TIMEOUT_SECONDS
    ) as http:
        pr = await http.get(f"/repos/{owner}/{repo}/pulls/{number}", headers=headers)
        if pr.status_code == 404:
            sys.exit(f"PR {owner}/{repo}#{number} not found, or not visible to this installation.")
        pr.raise_for_status()
        repo_response = await http.get(f"/repos/{owner}/{repo}", headers=headers)
        repo_response.raise_for_status()
    return pr.json(), repo_response.json()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="owner/repo")
    parser.add_argument("number", type=int, help="PR number")
    parser.add_argument("--action", default="opened", choices=["opened", "synchronize", "reopened"])
    parser.add_argument("--url", default="http://localhost:8000/webhooks/github")
    parser.add_argument(
        "--bad-signature",
        action="store_true",
        help="Corrupt the signature, to confirm the endpoint returns 401",
    )
    args = parser.parse_args()

    owner, _, repo = args.slug.partition("/")
    if not repo:
        sys.exit("slug must be owner/repo")

    installation_id = await _installation_id()
    pr, repository = await _fetch(owner, repo, args.number, installation_id)

    # Only the fields parse_pull_request_event reads. A real delivery carries far more;
    # sending a trimmed payload proves we do not silently depend on anything else.
    payload = {
        "action": args.action,
        "installation": {"id": installation_id},
        "repository": {
            "id": repository["id"],
            "full_name": repository["full_name"],
            "default_branch": repository.get("default_branch", "main"),
        },
        "pull_request": {
            "number": pr["number"],
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "draft": pr.get("draft", False),
            "user": {"login": (pr.get("user") or {}).get("login", ""),
                     "type": (pr.get("user") or {}).get("type", "User")},
            "head": {"sha": pr["head"]["sha"]},
            "base": {"sha": pr["base"]["sha"]},
        },
    }

    # Sign the exact bytes that will be sent. Re-serialising would change the MAC.
    raw = json.dumps(payload).encode()
    secret = get_settings().GITHUB_WEBHOOK_SECRET
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if args.bad_signature:
        digest = "0" * len(digest)

    print(f"{repository['full_name']}#{pr['number']}  {args.action}  head={pr['head']['sha'][:7]}")
    if pr.get("draft"):
        print("note: PR is a draft — expect a 200 'skipped' if SKIP_DRAFT_PRS is on")

    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.post(
            args.url,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": f"replay-{pr['head']['sha'][:12]}-{args.action}",
                "X-Hub-Signature-256": f"sha256={digest}",
                "User-Agent": "GitHub-Hookshot/replay",
            },
        )

    print(f"HTTP {response.status_code}  {response.text}")


if __name__ == "__main__":
    asyncio.run(main())

"""GitHub App authentication.

Two steps: sign a short-lived JWT with the App private key, then exchange it for an
installation access token. Installation tokens live about an hour and are cached in
Redis — never in Postgres, and never logged.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx
import jwt
import redis.asyncio as aioredis

from settings import get_settings

logger = logging.getLogger(__name__)

# Refresh this far before actual expiry so a token cannot expire mid-review.
_EXPIRY_MARGIN_SECONDS = 300
_JWT_LIFETIME_SECONDS = 540  # GitHub's ceiling is 10 minutes; stay under it


def build_app_jwt() -> str:
    """Sign an App-level JWT.

    `iat` is backdated 60s because GitHub rejects tokens issued in its future, and
    small clock skew between us and GitHub is normal. Requires pyjwt[crypto] — bare
    PyJWT cannot do RS256.
    """
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + _JWT_LIFETIME_SECONDS,
        "iss": settings.GITHUB_APP_ID,
    }
    return jwt.encode(payload, settings.github_private_key, algorithm="RS256")


async def get_installation_token(
    installation_id: int,
    redis: aioredis.Redis | None = None,
) -> str:
    """Return a cached or freshly minted installation token."""
    settings = get_settings()
    cache_key = f"gh:token:{installation_id}"
    client = redis or aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    owns_client = redis is None

    try:
        cached = await client.get(cache_key)
        if cached:
            return str(cached)

        token, ttl = await _mint_installation_token(installation_id)
        if ttl > 0:
            await client.set(cache_key, token, ex=ttl)
        return token
    finally:
        if owns_client:
            await client.aclose()


async def _mint_installation_token(installation_id: int) -> tuple[str, int]:
    settings = get_settings()
    url = f"{settings.GITHUB_API_URL}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {build_app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=settings.GITHUB_TIMEOUT_SECONDS) as http:
        response = await http.post(url, headers=headers)

    if response.status_code >= 400:
        # Deliberately does not include the response body or headers — an httpx error
        # repr will happily carry the Authorization header into the logs.
        raise GitHubAuthError(f"minting installation token failed: HTTP {response.status_code}")

    data: dict[str, Any] = response.json()
    token = str(data["token"])
    ttl = _ttl_from_expiry(str(data.get("expires_at", "")))
    logger.info("minted installation token", extra={"installation_id": installation_id})
    return token, ttl


def _ttl_from_expiry(expires_at: str) -> int:
    if not expires_at:
        return 0

    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    remaining = int(expiry.timestamp() - time.time()) - _EXPIRY_MARGIN_SECONDS
    return max(remaining, 0)


class GitHubAuthError(RuntimeError):
    """Raised when an installation token cannot be minted."""

"""GitHub REST client.

Scoped to what the slice needs: the PR, its changed files, and the content of those
files at the head commit. No repository clone — fetching the handful of changed files
over HTTP avoids a working tree, and therefore avoids leaking one on a hard kill.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx

from github.app_auth import get_installation_token
from settings import get_settings

logger = logging.getLogger(__name__)

RETRY_STATUS = frozenset({500, 502, 503, 504})
MAX_ATTEMPTS = 3

# GitHub signals a rate limit as 403 (historically) or 429 (increasingly). Both carry
# Retry-After on a secondary limit, so both must take the same path — treating 429 as a
# plain error was how a retryable pause became a failed review.
RATE_LIMIT_STATUS = frozenset({403, 429})

# Cap on a single honoured sleep. Beyond this the task should die and let Celery's
# backoff reschedule it, rather than hold a worker slot idle.
MAX_RETRY_AFTER_SECONDS = 60.0


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After, which is either delta-seconds or an HTTP-date.

    GitHub sends seconds, but the header is specified both ways and a ValueError here
    would turn a polite backoff into a crash.
    """
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _seconds_until_epoch(value: str) -> float | None:
    """How long until an x-ratelimit-reset epoch. Logging only."""
    try:
        return max(float(value) - datetime.now(UTC).timestamp(), 0.0)
    except ValueError:
        return None


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    """Async client authenticated as a specific installation.

    Use as a context manager so the underlying connection pool is closed:

        async with GitHubClient(installation_id) as gh:
            pr = await gh.get_pull_request("acme", "payments", 7)
    """

    def __init__(self, installation_id: int) -> None:
        self._installation_id = installation_id
        self._settings = get_settings()
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        token = await get_installation_token(self._installation_id)
        self._http = httpx.AsyncClient(
            base_url=self._settings.GITHUB_API_URL,
            timeout=self._settings.GITHUB_TIMEOUT_SECONDS,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "PRGuard/0.1",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("GitHubClient must be used as an async context manager")
        return self._http

    async def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """One request with retries on 5xx and rate limits.

        Secondary rate limits send `Retry-After` and must be honoured exactly —
        ignoring them escalates to an App-wide block, not just a slower response.
        """
        headers = {"Accept": accept} if accept else None
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(method, url, headers=headers, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if response.status_code in RATE_LIMIT_STATUS:
                delay = _retry_after_seconds(response.headers.get("retry-after"))
                if delay is not None and attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "secondary rate limit; sleeping",
                        extra={
                            "status": response.status_code,
                            "retry_after": delay,
                            "attempt": attempt,
                            "url": url,
                        },
                    )
                    await asyncio.sleep(min(delay, MAX_RETRY_AFTER_SECONDS))
                    continue

                if response.headers.get("x-ratelimit-remaining") == "0":
                    # Primary limit. The reset can be up to an hour out, so do not
                    # sleep on it — fail and let Celery's backoff reschedule.
                    reset = response.headers.get("x-ratelimit-reset", "")
                    logger.warning(
                        "primary rate limit exhausted",
                        extra={
                            "status": response.status_code,
                            "reset_epoch": reset,
                            "resets_in_seconds": _seconds_until_epoch(reset),
                            "url": url,
                        },
                    )
                    raise GitHubError("primary rate limit exhausted", response.status_code)

            return response

        raise GitHubError(f"{method} {url} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        response = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        if response.status_code == 404:
            raise GitHubError("pull request not found", 404)
        if response.status_code >= 400:
            raise GitHubError(
                f"fetching PR failed: HTTP {response.status_code}", response.status_code
            )
        data: dict[str, Any] = response.json()
        return data

    async def get_pull_files(
        self, owner: str, repo: str, number: int, *, max_files: int
    ) -> list[dict[str, Any]]:
        """Fetch changed files, following `Link` pagination up to `max_files`."""
        files: list[dict[str, Any]] = []
        page = 1
        while len(files) < max_files:
            response = await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{number}/files",
                params={"per_page": 100, "page": page},
            )
            if response.status_code >= 400:
                raise GitHubError(
                    f"fetching PR files failed: HTTP {response.status_code}",
                    response.status_code,
                )
            batch: list[dict[str, Any]] = response.json()
            if not batch:
                break
            files.extend(batch)
            if 'rel="next"' not in response.headers.get("link", ""):
                break
            page += 1
        return files[:max_files]

    async def get_file_content(
        self, owner: str, repo: str, path: str, ref: str, *, max_bytes: int
    ) -> str | None:
        """Return a file's text at `ref`, or None if missing, binary, or too large.

        Uses the raw media type so there is no base64 round-trip.
        """
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{path}",
            accept="application/vnd.github.raw",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            logger.warning("could not fetch %s: HTTP %s", path, response.status_code)
            return None
        if len(response.content) > max_bytes:
            logger.info("skipping %s: %d bytes exceeds cap", path, len(response.content))
            return None
        if b"\x00" in response.content[:8192]:
            return None  # binary
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def get_diff(self, owner: str, repo: str, number: int) -> str:
        """The whole PR as a unified diff, for storage in reviews.raw_diff."""
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}",
            accept="application/vnd.github.diff",
        )
        if response.status_code >= 400:
            raise GitHubError(
                f"fetching diff failed: HTTP {response.status_code}", response.status_code
            )
        return response.text

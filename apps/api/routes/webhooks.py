"""GitHub webhook intake.

Receives, verifies, records, enqueues, returns 202. Nothing slow happens here: no
GitHub API calls, no diff parsing, no LLM. GitHub times deliveries out at 10 seconds
and disables endpoints that fail persistently.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_sessionmaker
from github.webhooks import PullRequestEvent, parse_pull_request_event, verify_signature
from settings import get_settings

from apps.worker.celery_app import celery_app


logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> Response:
    settings = get_settings()

    # Raw bytes, before any parsing — see verify_signature.
    raw_body = await request.body()

    if not verify_signature(raw_body, x_hub_signature_256, settings.GITHUB_WEBHOOK_SECRET):
        logger.warning(
            "rejected webhook: bad signature",
            extra={
                "delivery": x_github_delivery,
                "client": request.client.host if request.client else None,
            },
        )
        return JSONResponse(
            {"detail": "invalid signature"}, status_code=status.HTTP_401_UNAUTHORIZED
        )

    try:
        payload: dict[str, Any] = await request.json()
    except ValueError:
        return JSONResponse(
            {"detail": "malformed json"}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
        )

    parsed = parse_pull_request_event(
        x_github_event or "",
        payload,
        skip_drafts=settings.SKIP_DRAFT_PRS,
        skip_bots=settings.SKIP_BOT_AUTHORS,
    )

    if not isinstance(parsed, PullRequestEvent):
        logger.info("skipping delivery", extra={"reason": parsed.reason, "detail": parsed.detail})
        # 200, not an error: a valid event we deliberately ignore.
        return JSONResponse(
            {"status": "skipped", "reason": parsed.reason}, status_code=status.HTTP_200_OK
        )

    async with get_sessionmaker()() as session:
        review_id, created = await _upsert_review(session, parsed, x_github_delivery)
        await session.commit()

    if created:
        _enqueue(review_id)
        logger.info(
            "review queued",
            extra={
                "review_id": str(review_id),
                "repo": parsed.repository_full_name,
                "pr": parsed.pr_number,
                "sha": parsed.head_sha[:7],
            },
        )
    else:
        # A redelivery, or a duplicate synchronize burst from a rebase. Enqueueing
        # again would pay for a second review of a commit already being reviewed.
        logger.info("duplicate delivery ignored", extra={"review_id": str(review_id)})

    return JSONResponse(
        {"status": "queued" if created else "duplicate", "review_id": str(review_id)},
        status_code=status.HTTP_202_ACCEPTED,
    )


async def _upsert_review(
    session: AsyncSession,
    event: PullRequestEvent,
    delivery_id: str | None,
) -> tuple[uuid.UUID, bool]:
    """Insert the review, or return the existing one.

    `xmax = 0` is true only for a freshly inserted row, which is how we distinguish a
    new review from a redelivery without a second query.
    """
    new_id = uuid.uuid4()
    result = await session.execute(
        text(
            """
            INSERT INTO reviews (
                id, installation_id, repository_id, repository_full_name,
                pr_number, head_sha, base_sha, delivery_id, event_action,
                status, title, description,
                changed_files, added_lines, deleted_lines, is_partial,
                input_tokens, output_tokens, cache_read_tokens,
                cache_creation_tokens, cost_usd
            ) VALUES (
                :id, :installation_id, :repository_id, :repository_full_name,
                :pr_number, :head_sha, :base_sha, :delivery_id, :event_action,
                'queued', :title, :description,
                0, 0, 0, false,
                0, 0, 0, 0, 0
            )
            ON CONFLICT (repository_id, pr_number, head_sha) DO UPDATE
                SET updated_at = now()
            RETURNING id, (xmax = 0) AS inserted
            """
        ),
        {
            "id": new_id,
            "installation_id": event.installation_id,
            "repository_id": event.repository_id,
            "repository_full_name": event.repository_full_name,
            "pr_number": event.pr_number,
            "head_sha": event.head_sha,
            "base_sha": event.base_sha,
            "delivery_id": delivery_id,
            "event_action": event.action,
            "title": event.title[:1000],
            "description": event.body[:20000],
        },
    )
    row = result.one()
    return row.id, bool(row.inserted)


def _enqueue(review_id: uuid.UUID) -> None:
    """Hand the review to the worker.

    Only the ID travels — not the payload. The worker reads current state from
    Postgres, so a retry after a code change does not replay a stale snapshot.
    """
    
    celery_app.send_task(
        "review.pull_request",
        args=[str(review_id)],
        queue="reviews",
        task_id=f"review:{review_id}",
    )

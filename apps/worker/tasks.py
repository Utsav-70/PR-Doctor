"""Celery tasks — the review pipeline.

Celery is sync; the pipeline is async. One `asyncio.run` per task at the boundary,
never nested.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, text

from agent.context import build_context
from agent.reviewer import review_diff
from agent.schemas import SEVERITY_ORDER
from agent.schemas import Finding as FindingSchema
from apps.worker.celery_app import celery_app
from db.models import Finding, Review, ReviewFile, ReviewStatus
from db.session import session_scope
from domain import PullRequestContext
from github.analyze import build_pull_request_context
from github.classify import looks_generated
from github.client import GitHubClient, GitHubError
from settings import get_settings

logger = logging.getLogger(__name__)


@celery_app.task(name="health.ping")
def ping() -> dict[str, Any]:
    """Prove the worker can reach the broker and the database."""
    return asyncio.run(_ping())


async def _ping() -> dict[str, Any]:
    async with session_scope() as session:
        value = (await session.execute(text("SELECT 1"))).scalar_one()
    return {"status": "ok", "database": value == 1}


@celery_app.task(
    name="review.pull_request",
    bind=True,
    acks_late=True,
    # KNOWN GAP (Phase 13 — Reliability): these three do nothing today.
    #
    # Celery applies `max_retries` / `retry_backoff` only when a task calls
    # `self.retry()` or declares `autoretry_for=(...)`. This task does neither, so an
    # exception propagates, the task is marked FAILURE, and no second attempt happens.
    # The settings read as if retries are on. They are not.
    #
    # The fix is NOT `autoretry_for=(Exception,)` — phase-13-reliability.md lists that
    # under Risks: it retries bugs and wastes a full paid review per failure. What is
    # needed is the classified list that phase specifies: retry transient failures
    # (5xx, timeouts, secondary rate limits), never retry 404s, refusals, or primary
    # rate limits whose reset is an hour out.
    #
    # Left inert deliberately rather than removed: the parameters are the ones Phase 13
    # will use, and deleting them would hide that the decision is outstanding.
    max_retries=2,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def review_pull_request(_self: Any, review_id: str) -> dict[str, Any]:
    return asyncio.run(_review(uuid.UUID(review_id)))


async def _review(review_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        review = (
            await session.execute(select(Review).where(Review.id == review_id))
        ).scalar_one_or_none()
        if review is None:
            logger.error("review not found", extra={"review_id": str(review_id)})
            return {"status": "missing"}
        # Commit `running` in its own transaction so a hung task is visible.
        # `running` -> `running` is legal here: acks_late means a worker killed
        # mid-review has its message redelivered, and the row is still RUNNING when
        # the replacement picks it up. The attempt counter is what distinguishes
        # "in progress" from "in progress for the third time".
        review.status = ReviewStatus.RUNNING
        review.attempt += 1
        review.started_at = datetime.now(UTC)
        attempt = review.attempt
        owner, repo_name = review.repository_full_name.split("/", 1)
        installation_id = review.installation_id
        pr_number = review.pr_number
        head_sha = review.head_sha

    if attempt > 1:
        logger.warning(
            "review retried",
            extra={"review_id": str(review_id), "attempt": attempt},
        )

    try:
        outcome = await _run_pipeline(
            review_id=review_id,
            installation_id=installation_id,
            owner=owner,
            repo=repo_name,
            pr_number=pr_number,
            expected_head_sha=head_sha,
        )
    except GitHubError as exc:
        if exc.status_code == 404:
            await _finish(review_id, ReviewStatus.SKIPPED, partial_reason="pr_not_found")
            return {"status": "skipped", "reason": "pr_not_found"}
        await _fail(review_id, "github_error", str(exc))
        raise
    except Exception as exc:
        await _fail(review_id, type(exc).__name__, str(exc))
        raise

    return outcome


async def _run_pipeline(
    *,
    review_id: uuid.UUID,
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    expected_head_sha: str,
) -> dict[str, Any]:
    settings = get_settings()

    # --- fetch -----------------------------------------------------------------
    async with GitHubClient(installation_id) as gh:
        pr = await gh.get_pull_request(owner, repo, pr_number)

        current_sha = str(pr["head"]["sha"])
        if current_sha != expected_head_sha:
            # A newer commit landed. That push has its own review; spending a full
            # review on a commit nobody will look at is waste.
            logger.info("superseded", extra={"review_id": str(review_id)})
            await _finish(review_id, ReviewStatus.SKIPPED, partial_reason="superseded")
            return {"status": "skipped", "reason": "superseded"}

        files_payload = await gh.get_pull_files(
            owner, repo, pr_number, max_files=settings.MAX_CHANGED_FILES
        )
        raw_diff = await gh.get_diff(owner, repo, pr_number)

        # --- analyse (Phase 3) -------------------------------------------------
        # Deterministic and network-free; everything below this line operates on the
        # context rather than on raw API payloads.
        ctx = build_pull_request_context(
            pr=pr,
            files_payload=files_payload,
            repository_full_name=f"{owner}/{repo}",
            ignore_globs=settings.ignore_globs,
            max_changed_lines=settings.MAX_CHANGED_LINES,
        )

        file_contents: dict[str, str] = {}
        for file in ctx.reviewed_files:
            content = await gh.get_file_content(
                owner, repo, file.path, expected_head_sha, max_bytes=settings.MAX_FILE_BYTES
            )
            if content is None:
                # Unreadable, oversized, or binary — the classifier could not know
                # that from the path alone.
                file.reviewed = False
                file.skip_reason = "content_unavailable"
                continue
            if looks_generated(content):
                # The content sniff that no path pattern catches. Must run here
                # rather than in the renderer, so the skip is recorded.
                file.reviewed = False
                file.skip_reason = "generated"
                continue
            file_contents[file.path] = content

    await _persist_files(review_id, ctx)

    if not ctx.reviewed_files:
        await _finish(
            review_id,
            ReviewStatus.SKIPPED,
            partial_reason="nothing_reviewable",
            changed_files=ctx.changed_files,
            added_lines=ctx.added_lines,
            deleted_lines=ctx.deleted_lines,
            raw_diff=raw_diff[: settings.MAX_DIFF_STORE_BYTES],
        )
        return {"status": "skipped", "reason": "nothing_reviewable"}

    # --- build context ---------------------------------------------------------
    bundle = build_context(
        title=ctx.title,
        description=ctx.description,
        files=ctx.reviewed_files,
        file_contents=file_contents,
        max_chars=settings.MAX_CONTEXT_CHARS,
    )

    # --- review ----------------------------------------------------------------
    result = await review_diff(bundle.repository_block, bundle.diff_block)

    # --- validate and persist --------------------------------------------------
    line_maps = {f.path: f.diff_line_map for f in ctx.reviewed_files}
    added_sets = {f.path: f.added_line_numbers for f in ctx.reviewed_files}
    kept, dropped = _validate(result.report.findings, line_maps, added_sets)

    async with session_scope() as session:
        review = (await session.execute(select(Review).where(Review.id == review_id))).scalar_one()
        review.title = ctx.title[:1000]
        review.description = ctx.description[:20000]
        review.changed_files = ctx.changed_files
        review.added_lines = ctx.added_lines
        review.deleted_lines = ctx.deleted_lines
        review.is_partial = ctx.is_partial or bundle.truncated
        review.partial_reason = ctx.partial_reason or (
            "context_truncated" if bundle.truncated else None
        )
        review.raw_diff = raw_diff[: settings.MAX_DIFF_STORE_BYTES]
        review.input_tokens = result.usage.input_tokens
        review.output_tokens = result.usage.output_tokens
        review.cache_read_tokens = result.usage.cache_read_tokens
        review.cache_creation_tokens = result.usage.cache_creation_tokens
        review.cost_usd = Decimal(str(round(result.usage.cost_usd, 6)))
        review.status = ReviewStatus.PARTIAL if review.is_partial else ReviewStatus.COMPLETED
        review.finished_at = datetime.now(UTC)
        if result.refused:
            review.error = {"type": "refusal", "stage": "reviewer"}

        for finding, position in kept:
            session.add(
                Finding(
                    review_id=review_id,
                    file_path=finding.file,
                    line=finding.line,
                    diff_position=position,
                    severity=finding.severity,
                    category=finding.category,
                    title=finding.title[:200],
                    description=finding.description,
                    confidence=Decimal(str(round(finding.confidence, 3))),
                    dropped=False,
                )
            )
        for finding, reason in dropped:
            session.add(
                Finding(
                    review_id=review_id,
                    file_path=finding.file,
                    line=finding.line,
                    diff_position=None,
                    severity=finding.severity,
                    category=finding.category,
                    title=finding.title[:200],
                    description=finding.description,
                    confidence=Decimal(str(round(finding.confidence, 3))),
                    dropped=True,
                    drop_reason=reason,
                )
            )

    logger.info(
        "review finished",
        extra={
            "review_id": str(review_id),
            "kept": len(kept),
            "dropped": len(dropped),
            "cost_usd": round(result.usage.cost_usd, 4),
        },
    )
    return {
        "status": "completed",
        "findings": len(kept),
        "dropped": len(dropped),
        "cost_usd": round(result.usage.cost_usd, 6),
    }


async def _persist_files(review_id: uuid.UUID, ctx: PullRequestContext) -> None:
    """Write one row per changed file, replacing any from an earlier attempt.

    Written before the LLM call, not after: if the review then fails, the analysis is
    still on record and answers "what did it decide to look at, and why" without a
    re-fetch. Delete-then-insert rather than upsert — a retry re-analyses the whole
    commit, so a file that vanished from the payload should vanish from the table.
    """
    async with session_scope() as session:
        await session.execute(delete(ReviewFile).where(ReviewFile.review_id == review_id))
        for file in ctx.files:
            session.add(
                ReviewFile(
                    review_id=review_id,
                    path=file.path[:1024],
                    previous_path=file.previous_path[:1024] if file.previous_path else None,
                    change_type=file.change_type,
                    language=file.language,
                    category=file.category,
                    added_lines=file.additions,
                    deleted_lines=file.deletions,
                    patch=file.patch,
                    # JSONB keys are strings; the model re-reads them as ints.
                    diff_line_map={str(k): v for k, v in file.diff_line_map.items()},
                    reviewed=file.reviewed,
                    skip_reason=file.skip_reason,
                )
            )


def _validate(
    findings: list[FindingSchema],
    line_maps: dict[str, dict[int, int]],
    added_lines: dict[str, set[int]],
) -> tuple[list[tuple[FindingSchema, int]], list[tuple[FindingSchema, str]]]:
    """Drop findings that cannot be anchored to a changed line.

    Structured output guarantees the shape; it guarantees nothing about whether the
    cited line exists or belongs to this PR. A confident observation about a line
    nobody touched is the most common way an LLM reviewer looks broken.
    """
    kept: list[tuple[FindingSchema, int]] = []
    dropped: list[tuple[FindingSchema, str]] = []
    seen: set[tuple[str, int, str]] = set()

    for finding in sorted(
        findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.confidence)
    ):
        if finding.file not in line_maps:
            dropped.append((finding, "unknown_file"))
            continue
        position = line_maps[finding.file].get(finding.line)
        if position is None:
            dropped.append((finding, "outside_diff"))
            continue
        if finding.line not in added_lines.get(finding.file, set()):
            dropped.append((finding, "unchanged_line"))
            continue
        key = (finding.file, finding.line, finding.category)
        if key in seen:
            dropped.append((finding, "duplicate"))
            continue
        seen.add(key)
        kept.append((finding, position))

    return kept, dropped


async def _finish(
    review_id: uuid.UUID,
    status: str,
    *,
    partial_reason: str | None = None,
    changed_files: int | None = None,
    added_lines: int | None = None,
    deleted_lines: int | None = None,
    raw_diff: str | None = None,
) -> None:
    async with session_scope() as session:
        review = (await session.execute(select(Review).where(Review.id == review_id))).scalar_one()
        review.status = status
        review.partial_reason = partial_reason
        review.finished_at = datetime.now(UTC)
        if changed_files is not None:
            review.changed_files = changed_files
        if added_lines is not None:
            review.added_lines = added_lines
        if deleted_lines is not None:
            review.deleted_lines = deleted_lines
        if raw_diff is not None:
            review.raw_diff = raw_diff


async def _fail(review_id: uuid.UUID, error_type: str, message: str) -> None:
    async with session_scope() as session:
        review = (await session.execute(select(Review).where(Review.id == review_id))).scalar_one()
        review.status = ReviewStatus.FAILED
        review.finished_at = datetime.now(UTC)
        # The attempt is recorded so a failure that only appears on retry — a stale
        # token, a superseded SHA — is distinguishable from one that failed outright.
        review.error = {
            "type": error_type,
            "message": message[:2000],
            "attempt": review.attempt,
        }

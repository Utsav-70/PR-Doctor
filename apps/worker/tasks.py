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

from sqlalchemy import select, text

from agent.context import build_context, is_ignored, is_source
from agent.reviewer import review_diff
from agent.schemas import SEVERITY_ORDER
from agent.schemas import Finding as FindingSchema
from apps.worker.celery_app import celery_app
from db.models import Finding, Review, ReviewStatus
from db.session import session_scope
from github.client import GitHubClient, GitHubError
from github.diff import FileDiff, parse_files
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
        review.status = ReviewStatus.RUNNING
        review.started_at = datetime.now(UTC)
        owner, repo_name = review.repository_full_name.split("/", 1)
        installation_id = review.installation_id
        pr_number = review.pr_number
        head_sha = review.head_sha

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

        diffs = parse_files(files_payload)
        reviewable = _select_reviewable(diffs, settings.ignore_globs)

        file_contents: dict[str, str] = {}
        for diff in reviewable:
            content = await gh.get_file_content(
                owner, repo, diff.path, expected_head_sha, max_bytes=settings.MAX_FILE_BYTES
            )
            if content is not None:
                file_contents[diff.path] = content

    added = sum(d.additions for d in diffs)
    deleted = sum(d.deletions for d in diffs)
    changed_lines = sum(d.additions + d.deletions for d in reviewable)

    is_partial = False
    partial_reason: str | None = None
    if changed_lines > settings.MAX_CHANGED_LINES:
        is_partial = True
        partial_reason = "diff_too_large"
        reviewable = _trim_to_budget(reviewable, settings.MAX_CHANGED_LINES)

    if not reviewable:
        await _finish(
            review_id,
            ReviewStatus.SKIPPED,
            partial_reason="nothing_reviewable",
            changed_files=len(diffs),
            added_lines=added,
            deleted_lines=deleted,
            raw_diff=raw_diff[: settings.MAX_DIFF_STORE_BYTES],
        )
        return {"status": "skipped", "reason": "nothing_reviewable"}

    # --- build context ---------------------------------------------------------
    bundle = build_context(
        title=str(pr.get("title") or ""),
        description=str(pr.get("body") or ""),
        files=reviewable,
        file_contents=file_contents,
        max_chars=settings.MAX_CONTEXT_CHARS,
    )

    # --- review ----------------------------------------------------------------
    result = await review_diff(bundle.repository_block, bundle.diff_block)

    # --- validate and persist --------------------------------------------------
    line_maps = {d.path: d.line_map for d in reviewable}
    added_sets = {d.path: d.added_lines for d in reviewable}
    kept, dropped = _validate(result.report.findings, line_maps, added_sets)

    async with session_scope() as session:
        review = (await session.execute(select(Review).where(Review.id == review_id))).scalar_one()
        review.title = str(pr.get("title") or "")[:1000]
        review.description = str(pr.get("body") or "")[:20000]
        review.changed_files = len(diffs)
        review.added_lines = added
        review.deleted_lines = deleted
        review.is_partial = is_partial or bundle.truncated
        review.partial_reason = partial_reason or (
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


def _select_reviewable(diffs: list[FileDiff], ignore_globs: list[str]) -> list[FileDiff]:
    return [
        d
        for d in diffs
        if d.is_reviewable and is_source(d.path) and not is_ignored(d.path, ignore_globs)
    ]


def _trim_to_budget(diffs: list[FileDiff], budget: int) -> list[FileDiff]:
    """Keep the smallest files first — more files reviewed per token spent."""
    kept: list[FileDiff] = []
    spent = 0
    for diff in sorted(diffs, key=lambda d: d.additions + d.deletions):
        cost = diff.additions + diff.deletions
        if spent + cost > budget:
            continue
        kept.append(diff)
        spent += cost
    return kept


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
        review.error = {"type": error_type, "message": message[:2000]}

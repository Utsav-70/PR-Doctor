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

from agent.graph import UsageAccumulator, run_review_graph
from agent.schemas import CallRecord
from agent.tools.gateway import ToolGateway
from agent.tools.workspace import PathNotAllowed, Workspace
from apps.worker.celery_app import celery_app
from db.models import Finding, LlmCall, Review, ReviewFile, ReviewStatus
from db.session import session_scope
from domain import PullRequestContext
from github.analyze import build_pull_request_context
from github.checkout import checkout
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

    # Phase 4 replaced the per-file HTTP fetch loop that used to sit here: the checkout
    # below gives the tools a working tree, so reading a file is a filesystem call
    # rather than one GitHub request each.

    if not ctx.reviewed_files:
        await _persist_files(review_id, ctx)
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

    # --- checkout, investigate, review, validate (Phases 4 and 5) ---------------
    # The tree is deleted when this block exits, on every path including failure. A
    # leaked clone of a private repository is an incident.
    async with checkout(
        installation_id=installation_id, owner=owner, repo=repo, sha=expected_head_sha
    ) as root:
        workspace = Workspace.at(
            root, repository_full_name=f"{owner}/{repo}", head_sha=expected_head_sha
        )
        _flag_generated_files(workspace, ctx)
        await _persist_files(review_id, ctx)

        gateway = ToolGateway(workspace, review_id=review_id)
        state = await run_review_graph(review_id=review_id, pr=ctx, gateway=gateway)

    kept = state.get("findings", [])
    dropped = [(d.finding, d.reason) for d in state.get("dropped", [])]
    usage = state.get("usage") or UsageAccumulator()
    errors = state.get("errors", [])
    bundle = state.get("context_bundle")
    truncated = bool(bundle and bundle.truncated)

    await _persist_llm_calls(review_id, usage.calls)

    async with session_scope() as session:
        review = (await session.execute(select(Review).where(Review.id == review_id))).scalar_one()
        review.title = ctx.title[:1000]
        review.description = ctx.description[:20000]
        review.changed_files = ctx.changed_files
        review.added_lines = ctx.added_lines
        review.deleted_lines = ctx.deleted_lines
        review.is_partial = ctx.is_partial or truncated
        review.partial_reason = ctx.partial_reason or ("context_truncated" if truncated else None)
        review.raw_diff = raw_diff[: settings.MAX_DIFF_STORE_BYTES]
        review.input_tokens = usage.input_tokens
        review.output_tokens = usage.output_tokens
        review.cache_read_tokens = usage.cache_read_tokens
        review.cache_creation_tokens = usage.cache_creation_tokens
        review.cost_usd = Decimal(str(round(usage.cost_usd, 6)))
        review.status = ReviewStatus.PARTIAL if review.is_partial else ReviewStatus.COMPLETED
        review.finished_at = datetime.now(UTC)
        if errors:
            # Degraded stages, not a failed review. Recorded so "why were there no
            # findings" has an answer that is not guesswork.
            review.error = {
                "type": "degraded",
                "stages": [{"stage": e.stage, "kind": e.kind, "detail": e.detail} for e in errors],
            }

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
            "llm_calls": len(usage.calls),
            "tool_calls": gateway.usage.calls,
            "tool_iterations": state.get("tool_iterations", 0),
            "cost_usd": round(usage.cost_usd, 4),
        },
    )
    return {
        "status": "completed",
        "findings": len(kept),
        "dropped": len(dropped),
        "llm_calls": len(usage.calls),
        "tool_calls": gateway.usage.calls,
        "cost_usd": round(usage.cost_usd, 6),
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


def _flag_generated_files(workspace: Workspace, ctx: PullRequestContext) -> None:
    """Content-sniff for generated files, now that a working tree exists.

    Runs against the checkout rather than an HTTP fetch, and marks the skip on the
    context so it lands in `review_files` — a file excluded for having an `@generated`
    header should be as visible as one excluded by an ignore glob.
    """
    for file in ctx.reviewed_files:
        try:
            target = workspace.resolve(file.path)
        except PathNotAllowed:
            file.reviewed = False
            file.skip_reason = "path_not_allowed"
            continue
        if not target.is_file():
            file.reviewed = False
            file.skip_reason = "missing_at_head"
            continue
        try:
            head = target.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            file.reviewed = False
            file.skip_reason = "content_unavailable"
            continue
        if looks_generated(head):
            file.reviewed = False
            file.skip_reason = "generated"


async def _persist_llm_calls(review_id: uuid.UUID, calls: list[CallRecord]) -> None:
    """One row per provider request.

    Written before the review row is finalised, so a failure between the two still
    leaves the spend on record. Cost you cannot see is cost you cannot control.
    """
    if not calls:
        return
    async with session_scope() as session:
        for call in calls:
            session.add(
                LlmCall(
                    review_id=review_id,
                    stage=call.stage[:32],
                    provider=call.provider[:16],
                    model=call.model[:64],
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_creation_tokens=call.cache_creation_tokens,
                    cost_usd=Decimal(str(round(call.cost_usd, 6))),
                    stop_reason=call.stop_reason[:32] if call.stop_reason else None,
                    duration_ms=call.duration_ms,
                    tool_iterations=call.tool_iterations,
                    error=call.error[:512] if call.error else None,
                )
            )


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

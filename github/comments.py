"""Phase 9 — turning findings into a GitHub review.

Three separable jobs, in order:

    1. guardrails   deterministic checks; nothing reaches the API without passing
    2. rendering    a comment body, and a summary
    3. posting      one review, one request

The guardrails deliberately repeat checks the validation stage already made. This is
the last gate before an irreversible, developer-visible action, and it is deterministic
where the upstream stages are not. Redundancy at a trust boundary is not waste.

Nothing here is async except the posting call, so rendering and guardrails can be
exercised without a network or a database.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent.schemas import SEVERITY_ORDER
from agent.schemas import Finding as FindingSchema
from domain import PullRequestContext
from settings import get_settings

logger = logging.getLogger(__name__)

# GitHub rejects a comment body over this. Truncate with a marker rather than dropping
# the finding — a long description is still worth reading.
MAX_COMMENT_CHARS = 65_536
MAX_DESCRIPTION_CHARS = 4_000

SEVERITY_MARKER: dict[str, str] = {
    "CRITICAL": "🔴",
    "HIGH": "🔴",
    "MEDIUM": "🟠",
    "LOW": "🟡",
    "INFO": "🔵",
}


@dataclass(frozen=True, slots=True)
class Anchored:
    """A finding that passed every guardrail, with the diff position it will land on."""

    finding: FindingSchema
    position: int
    body: str

    def as_comment(self) -> dict[str, Any]:
        return {"path": self.finding.file, "position": self.position, "body": self.body}


@dataclass(frozen=True, slots=True)
class Blocked:
    finding: FindingSchema
    reason: str


@dataclass
class PostPlan:
    """Everything the posting step needs, and everything the DB needs to record."""

    anchored: list[Anchored] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)
    summary: str = ""
    trimmed: int = 0


def _inline_code(text: str) -> str:
    """Wrap in a backtick fence long enough to survive the content.

    File paths and identifiers come from the repository, which is attacker-controlled.
    A path containing a backtick would otherwise break out of the span and let repo
    content render as PRGuard's own prose.
    """
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _prose(text: str) -> str:
    """Model-authored prose, made inert as markup.

    The description quotes repository code, so it can carry `</details>`, an HTML
    comment, or an image tag. Escaping the three HTML-significant characters leaves
    markdown emphasis and backticks working — GitHub renders `&lt;` back as `<` — while
    making it impossible for quoted content to close a block we opened.
    """
    return html.escape(text, quote=False)


def render_comment(finding: FindingSchema, review_id: str) -> str:
    """One inline comment.

    No suggested-changes block. Auto-fix is a non-goal, and a wrong suggestion someone
    clicks is a worse outcome than a wrong comment they read.
    """
    marker = SEVERITY_MARKER.get(finding.severity, "⚪")
    description = finding.description[:MAX_DESCRIPTION_CHARS]
    truncated = len(finding.description) > MAX_DESCRIPTION_CHARS

    parts = [
        f"**{marker} {finding.severity} · {finding.category}** · confidence {finding.confidence:.2f}",
        "",
        _prose(finding.title),
        "",
        _prose(description) + ("\n\n_(description truncated)_" if truncated else ""),
        "",
        f"<sub>PRGuard · review {_inline_code(review_id[:8])}</sub>",
    ]
    body = "\n".join(parts)
    if len(body) > MAX_COMMENT_CHARS:
        body = body[: MAX_COMMENT_CHARS - 20].rstrip() + "\n\n_(truncated)_"
    return body


# Skip reasons, as a developer would describe them. Anything unmapped falls back to the
# raw reason rather than being dropped — an unnamed exclusion is still an exclusion.
SKIP_REASON_LABEL: dict[str, str] = {
    "budget": "over the diff-size budget",
    "ignored_path": "on an ignored path",
    "generated": "generated",
    "binary": "binary",
    "deleted": "deleted",
    "no_patch": "no diff available",
    "missing_at_head": "missing at this commit",
    "path_not_allowed": "outside the repository",
    "content_unavailable": "unreadable",
    "category_documentation": "documentation",
    "category_config": "configuration",
    "category_data": "data",
}


def _coverage(pr: PullRequestContext) -> str:
    """Every file that was not reviewed, grouped by why.

    Counting only `budget` skips understated this badly: a 50-file PR with 25 reviewed
    reported "9 were not analysed", because the other 16 were excluded for other reasons
    and silently dropped out of the sentence. A partial-review warning that undercounts
    what it missed is the exact failure it exists to prevent.
    """
    skipped = pr.skipped_files
    head = f"Reviewed {len(pr.reviewed_files)} of {pr.changed_files} files."
    if not skipped:
        return head

    by_reason: dict[str, int] = {}
    for file in skipped:
        by_reason[file.skip_reason or "unspecified"] = (
            by_reason.get(file.skip_reason or "unspecified", 0) + 1
        )
    parts = [
        f"{count} {SKIP_REASON_LABEL.get(reason, reason)}"
        for reason, count in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return f"{head} The other {len(skipped)} were skipped: {', '.join(parts)}."


def render_summary(
    pr: PullRequestContext,
    *,
    posted: int,
    trimmed: int,
    duration_seconds: float | None = None,
) -> str:
    """Short, factual, and only rendered when there is something to post.

    The partial-review warning is not optional: a review that presents itself as
    complete when it examined a quarter of the diff is a correctness bug, not a
    cosmetic one.
    """
    timing = f" in {duration_seconds:.0f}s" if duration_seconds else ""
    reviewed = len(pr.reviewed_files)
    lines = [
        f"Reviewed {reviewed} file{'s' if reviewed != 1 else ''} "
        f"(+{pr.added_lines} -{pr.deleted_lines}){timing} · "
        f"{posted} finding{'s' if posted != 1 else ''}"
    ]

    if pr.is_partial:
        lines += ["", f"⚠️ **Partial review** ({pr.partial_reason or 'budget'}). {_coverage(pr)}"]

    if trimmed:
        lines += [
            "",
            f"⚠️ {trimmed} further finding{'s' if trimmed != 1 else ''} not shown "
            f"(comment limit). See the review record.",
        ]

    return "\n".join(lines)


def build_post_plan(
    findings: list[tuple[FindingSchema, int]],
    pr: PullRequestContext,
    *,
    review_id: str,
    already_posted: set[tuple[str, int, str]] | None = None,
    duration_seconds: float | None = None,
) -> PostPlan:
    """Apply every output guardrail, then render what survives.

    `findings` is the validated output of the review graph, already paired with the
    diff position validation resolved. Each check below re-derives rather than trusts.
    """
    settings = get_settings()
    plan = PostPlan()
    seen: set[tuple[str, int, str]] = set(already_posted or ())
    min_severity = SEVERITY_ORDER[settings.MIN_SEVERITY_TO_POST]
    by_path = {f.path: f for f in pr.files}

    # Most severe first, so if the comment cap trims anything it trims the least
    # important. Confidence breaks ties within a severity.
    ordered = sorted(
        findings, key=lambda fp: (SEVERITY_ORDER.get(fp[0].severity, 9), -fp[0].confidence)
    )

    for finding, position in ordered:
        reason = _blocked_reason(finding, position, by_path, seen, settings, min_severity)
        if reason is not None:
            # WARNING, not INFO: a guardrail firing means an upstream stage produced
            # something it should not have, or a threshold needs looking at.
            logger.warning(
                "finding blocked from posting: %s (%s:%d, %s, conf %.2f)",
                reason,
                finding.file,
                finding.line,
                finding.severity,
                finding.confidence,
            )
            plan.blocked.append(Blocked(finding, reason))
            continue

        seen.add((finding.file, finding.line, finding.category))
        plan.anchored.append(Anchored(finding, position, render_comment(finding, review_id)))

    if len(plan.anchored) > settings.MAX_COMMENTS_PER_REVIEW:
        plan.trimmed = len(plan.anchored) - settings.MAX_COMMENTS_PER_REVIEW
        for extra in plan.anchored[settings.MAX_COMMENTS_PER_REVIEW :]:
            plan.blocked.append(Blocked(extra.finding, "comment_limit"))
        plan.anchored = plan.anchored[: settings.MAX_COMMENTS_PER_REVIEW]

    plan.summary = render_summary(
        pr,
        posted=len(plan.anchored),
        trimmed=plan.trimmed,
        duration_seconds=duration_seconds,
    )
    return plan


@dataclass
class PostResult:
    posted: bool = False
    dry_run: bool = False
    skipped_reason: str | None = None
    github_review_id: int | None = None
    # (path, position) -> comment id, for tracing a finding to what a developer sees.
    comment_ids: dict[tuple[str, int], int] = field(default_factory=dict)


async def post_review(
    plan: PostPlan,
    *,
    installation_id: int,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> PostResult:
    """Publish the plan as one GitHub review, or explain why it did not.

    Zero findings posts nothing at all — not a "no issues found" comment. A bot that
    comments on every pull request trains people to ignore its comments, and the review
    record in the database is where "we did look" belongs.

    The exception is a partial review: the limitation is worth saying even with nothing
    to report, because a silently incomplete review is a correctness bug.
    """
    settings = get_settings()

    if not plan.anchored:
        logger.info("nothing to post")
        return PostResult(skipped_reason="no_findings")

    if settings.DRY_RUN:
        # The whole point of dry run is reading what it *would* have said, so the
        # bodies are logged in full rather than counted.
        logger.info(
            "DRY_RUN: would post %d comment%s",
            len(plan.anchored),
            "" if len(plan.anchored) == 1 else "s",
        )
        logger.info("DRY_RUN summary:\n%s", plan.summary)
        for item in plan.anchored:
            logger.info(
                "DRY_RUN comment %s @ position %d:\n%s",
                item.finding.file,
                item.position,
                item.body,
            )
        return PostResult(dry_run=True, skipped_reason="dry_run")

    from github.client import GitHubClient

    async with GitHubClient(installation_id) as gh:
        # Re-check immediately before posting, not just at fetch time. The review took
        # minutes; if a new commit landed in that window the diff positions no longer
        # refer to the diff GitHub will anchor against, and every comment would land on
        # the wrong line. The newer commit has its own review in flight.
        current = await gh.get_pull_request(owner, repo, pr_number)
        if str(current["head"]["sha"]) != head_sha:
            logger.warning(
                "not posting: head moved from %s to %s",
                head_sha[:7],
                str(current["head"]["sha"])[:7],
            )
            return PostResult(skipped_reason="superseded")

        response = await gh.create_review(
            owner,
            repo,
            pr_number,
            commit_id=head_sha,
            body=plan.summary,
            comments=[item.as_comment() for item in plan.anchored],
        )
        github_review_id = int(response["id"])

        comment_ids: dict[tuple[str, int], int] = {}
        for comment in await gh.list_review_comments(owner, repo, pr_number, github_review_id):
            position = comment.get("position") or comment.get("original_position")
            if position is not None:
                comment_ids[(str(comment["path"]), int(position))] = int(comment["id"])

    logger.info(
        "posted review %d with %d comments to %s/%s#%d",
        github_review_id,
        len(plan.anchored),
        owner,
        repo,
        pr_number,
    )
    return PostResult(posted=True, github_review_id=github_review_id, comment_ids=comment_ids)


def _blocked_reason(
    finding: FindingSchema,
    position: int,
    by_path: dict[str, Any],
    seen: set[tuple[str, int, str]],
    settings: Any,
    min_severity: int,
) -> str | None:
    """The guardrail chain, in order. First failure wins.

    Order matters for diagnosis: `below_threshold` on a finding that also cites an
    unknown file would hide the real problem behind a tuning knob.
    """
    file = by_path.get(finding.file)
    if file is None:
        return "unknown_file"
    if not file.reviewed:
        return "file_not_reviewed"
    # Re-derived from the line map rather than trusting the position handed in. This is
    # the check that prevents a comment landing on the wrong line, which is worse than
    # no comment: it makes the finding unverifiable and the bot look broken.
    if file.diff_line_map.get(finding.line) != position:
        return "outside_diff"
    if finding.line not in file.added_line_numbers:
        return "unchanged_line"
    if (finding.file, finding.line, finding.category) in seen:
        return "duplicate"
    if finding.confidence < settings.CONFIDENCE_THRESHOLD:
        return "below_threshold"
    if SEVERITY_ORDER.get(finding.severity, 9) > min_severity:
        return "below_threshold"
    if not finding.title.strip() or not finding.description.strip():
        return "empty_content"
    return None

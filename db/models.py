"""Database models.

Three tables. Installation and repository metadata is denormalised onto `reviews` —
normalising it costs two joins and buys nothing until there is a UI or per-repository
configuration to hang off it.

`review_files` is the exception, and it is normalised for a reason: it is the handoff
point between processes. The review task computes patches and line maps in memory; the
posting task (Phase 9) is a different process with empty memory and needs them back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ReviewStatus:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        # The idempotency key. A redelivered webhook upserts rather than starting a
        # second review of the same commit.
        UniqueConstraint("repository_id", "pr_number", "head_sha", name="uq_review_repo_pr_sha"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    installation_id: Mapped[int] = mapped_column(BigInteger)
    repository_id: Mapped[int] = mapped_column(BigInteger, index=True)
    repository_full_name: Mapped[str] = mapped_column(String(512))

    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(64))
    base_sha: Mapped[str] = mapped_column(String(64))

    delivery_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_action: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.QUEUED, index=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    changed_files: Mapped[int] = mapped_column(Integer, default=0)
    added_lines: Mapped[int] = mapped_column(Integer, default=0)
    deleted_lines: Mapped[int] = mapped_column(Integer, default=0)

    is_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    partial_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))

    # The unified diff as fetched, so a past review's input can be reconstructed
    # after the fact. This is the main post-hoc debugging tool: when a finding looks
    # wrong, it answers "what did the model actually see?"
    #
    # Two consequences of keeping it, both deliberate:
    #   - It is private repository source code at rest. Prunable by design — null it
    #     out on reviews older than the retention window; the review row stays intact.
    #   - A PR that commits a secret puts that secret here too. Once Phase 12 adds
    #     Gitleaks, redact on the way in, not just on the prompt path.
    #
    # Truncated to MAX_DIFF_STORE_BYTES on write, with a marker, so one enormous
    # generated-file diff cannot bloat the table.
    raw_diff: Mapped[str | None] = mapped_column(Text, nullable=True)

    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Incremented on redelivery by the worker, so `running` -> `queued` is a legal
    # transition rather than an anomaly. A review on attempt 3 is worth looking at.
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    findings: Mapped[list[Finding]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )
    files: Mapped[list[ReviewFile]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Review {self.repository_full_name}#{self.pr_number} "
            f"{self.head_sha[:7]} {self.status}>"
        )


class Finding(Base):
    """A single reported issue.

    Findings dropped by validation are kept with `dropped=True` and a reason. The drop
    distribution is the fastest way to diagnose a bad prompt, so nothing is discarded
    silently.
    """

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )

    file_path: Mapped[str] = mapped_column(String(1024))
    line: Mapped[int] = mapped_column(Integer)
    # Position in the unified diff — what GitHub's review API needs to anchor a
    # comment. Null when the finding never resolved to a diff line.
    diff_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))

    dropped: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    drop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Phase 6. Which specialist reported this. Per-agent precision is the number that
    # says whether a prompt is pulling its weight, and it cannot be recovered later.
    agent: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

    # A finding's life is recorded in four separate columns rather than one status,
    # because the four questions have different answers and conflating them hides the
    # one that matters:
    #
    #   drop_reason      validation rejected it as malformed        (Phase 5)
    #   verified         evidence ran and what it showed            (Phase 7)
    #   rejection_code   the judge declined to publish it           (Phase 8)
    #   blocked_reason   a guardrail declined to publish it         (Phase 9)
    #
    # A threshold tuned too tight and a bad prompt look identical in a single column.

    # Phase 7. `verified` is null when verification did not run (capped, or the severity
    # was below the floor) — distinct from false, which means it ran and proved nothing.
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verified_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)

    # Phase 8.
    judge_accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    judge_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Kept when the judge rewrites `description`. Comparing the two across a hundred
    # reviews is what tells you how the reviewer prompts are miscalibrated.
    description_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Hash of (file, line, category, normalised title). The key cross-review dedupe
    # matches on, so a PR with five pushes does not accumulate five copies of a comment.
    dedupe_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Indexed in the migration as a *partial* index (WHERE posted_at IS NOT NULL) —
    # `index=True` here would declare a full one and the two would disagree.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    review: Mapped[Review] = relationship(back_populates="findings")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Finding {self.severity} {self.file_path}:{self.line}>"


class ReviewFile(Base):
    """One changed file, as analysed.

    Exists so the per-file facts outlive the review task. Without it the patch, the
    classification, and the line map are computed, used once, and garbage-collected —
    which forces every later phase to re-fetch from GitHub to answer questions the
    pipeline already answered.
    """

    __tablename__ = "review_files"
    __table_args__ = (
        # A retry re-analyses the same commit, so the write must be an upsert rather
        # than an append. Without this, attempt 2 silently doubles every file.
        UniqueConstraint("review_id", "path", name="uq_review_file_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )

    path: Mapped[str] = mapped_column(String(1024))
    previous_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    change_type: Mapped[str] = mapped_column(String(16))

    # How to parse it, and what role it plays. Two orthogonal axes — see
    # github/classify.py for why they are not collapsed into one column.
    language: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))

    added_lines: Mapped[int] = mapped_column(Integer, default=0)
    deleted_lines: Mapped[int] = mapped_column(Integer, default=0)

    # Null when GitHub omitted it: binary, oversized, or a mode-only change. This is
    # the largest column in the schema and the first thing to prune on a retention
    # policy — it is regenerable from GitHub while the SHA still exists.
    patch: Mapped[str | None] = mapped_column(Text, nullable=True)

    # {new_file_line: diff_position}, JSON so the keys are strings on the way out.
    # GitHub's review API anchors comments to a diff position, not a file line, so
    # this is what Phase 9 reads instead of re-parsing the patch.
    diff_line_map: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)

    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Why not, when not: deleted, ignored_path, generated, binary, no_patch,
    # category_documentation, budget. Unset when the file was reviewed.
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    review: Mapped[Review] = relationship(back_populates="files")

    def __repr__(self) -> str:  # pragma: no cover
        flag = "reviewed" if self.reviewed else f"skipped:{self.skip_reason}"
        return f"<ReviewFile {self.path} {self.language}/{self.category} {flag}>"


class ToolCall(Base):
    """One tool invocation, including the denied ones.

    This is the answer to "why did it say that" in Phase 15. Denials matter most:
    `allowed = false` rows are how an over-broad permission set becomes visible now,
    while the tools only read — rather than in Phase 11, when they can execute.
    """

    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )

    # Which agent asked. One name in Phase 5; bug/security/performance from Phase 6.
    agent: Mapped[str] = mapped_column(String(32), index=True)
    tool: Mapped[str] = mapped_column(String(32), index=True)
    # Arguments as received. Model-supplied and therefore evidence: a path traversal
    # attempt is only diagnosable if the rejected argument was recorded.
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    allowed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    denied_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    result_bytes: Mapped[int] = mapped_column(Integer, default=0)
    result_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        verdict = "ok" if self.allowed and self.ok else (self.denied_reason or self.error)
        return f"<ToolCall {self.agent}/{self.tool} {verdict}>"


class LlmCall(Base):
    """One request to a model provider.

    Separate from `reviews` because Phase 6 fans out to three agents and Phase 8 adds a
    judge — a review will make many calls, and "which stage cost what" is the question
    Phase 14 optimises against. Aggregating onto the review row first would lose exactly
    the breakdown that makes the optimisation possible.
    """

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )

    # Which pipeline step asked. One value in Phase 5; bug/security/performance/judge
    # from Phase 6 onward.
    stage: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64), index=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))

    # Recorded per call rather than derived: a refusal and a max_tokens truncation both
    # return HTTP 200 with usable-looking output, and telling them apart afterwards is
    # impossible without this.
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    tool_iterations: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LlmCall {self.stage} {self.model} ${self.cost_usd}>"


class Evidence(Base):
    """One executed verification check.

    Refutations are kept as carefully as confirmations — more carefully, in fact. The set
    of refuted findings is where false-positive analysis starts, and it is how a prompt
    regression is told apart from a tooling one.

    `detail` is what makes a posted comment defensible: the comment can cite "no unique
    constraint on payments.idempotency_key" because a row here recorded exactly that.
    """

    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), index=True
    )

    method: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=0)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Evidence {self.method} {self.outcome} w={self.weight}>"

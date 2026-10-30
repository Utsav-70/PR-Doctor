"""llm_calls table

Phase 5. One row per model request, so per-stage cost is answerable once Phase 6 fans
out to several agents.

Revision ID: e3d9a05c1782
Revises: c7b2e8a41d55
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3d9a05c1782"
down_revision: str | None = "c7b2e8a41d55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("review_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("tool_iterations", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_calls_review_id", "llm_calls", ["review_id"])
    op.create_index("ix_llm_calls_stage", "llm_calls", ["stage"])
    op.create_index("ix_llm_calls_model", "llm_calls", ["model"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_model", table_name="llm_calls")
    op.drop_index("ix_llm_calls_stage", table_name="llm_calls")
    op.drop_index("ix_llm_calls_review_id", table_name="llm_calls")
    op.drop_table("llm_calls")

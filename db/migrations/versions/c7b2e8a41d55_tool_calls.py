"""tool_calls audit table

Phase 4. One row per tool invocation, including denials.

Revision ID: c7b2e8a41d55
Revises: a1f4c7d92b03
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7b2e8a41d55"
down_revision: str | None = "a1f4c7d92b03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("review_id", sa.UUID(), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column(
            "args", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("denied_reason", sa.String(length=64), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("result_bytes", sa.Integer(), nullable=False),
        sa.Column("result_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_calls_review_id", "tool_calls", ["review_id"])
    op.create_index("ix_tool_calls_agent", "tool_calls", ["agent"])
    op.create_index("ix_tool_calls_tool", "tool_calls", ["tool"])
    # Denials are the rows you go looking for — an over-broad permission set shows up
    # here long before it becomes dangerous in Phase 11.
    op.create_index("ix_tool_calls_allowed", "tool_calls", ["allowed"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_allowed", table_name="tool_calls")
    op.drop_index("ix_tool_calls_tool", table_name="tool_calls")
    op.drop_index("ix_tool_calls_agent", table_name="tool_calls")
    op.drop_index("ix_tool_calls_review_id", table_name="tool_calls")
    op.drop_table("tool_calls")

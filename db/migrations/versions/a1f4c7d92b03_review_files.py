"""review_files table and reviews.attempt

Adds the per-file analysis table that Phases 2 and 3 specify, plus the retry counter.

Revision ID: a1f4c7d92b03
Revises: 93f15307ea9f
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1f4c7d92b03"
down_revision: str | None = "93f15307ea9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_files",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("review_id", sa.UUID(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("previous_path", sa.String(length=1024), nullable=True),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("added_lines", sa.Integer(), nullable=False),
        sa.Column("deleted_lines", sa.Integer(), nullable=False),
        sa.Column("patch", sa.Text(), nullable=True),
        sa.Column(
            "diff_line_map",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("reviewed", sa.Boolean(), nullable=False),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_id", "path", name="uq_review_file_path"),
    )
    op.create_index("ix_review_files_review_id", "review_files", ["review_id"])
    op.create_index("ix_review_files_reviewed", "review_files", ["reviewed"])

    # server_default so the column is backfilled on existing rows; the model keeps a
    # Python-side default too, for objects constructed outside the DB.
    op.add_column(
        "reviews",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("reviews", "attempt")
    op.drop_index("ix_review_files_reviewed", table_name="review_files")
    op.drop_index("ix_review_files_review_id", table_name="review_files")
    op.drop_table("review_files")

"""findings posting columns

Phase 9. Records whether a finding reached a developer, and if not, which output
guardrail stopped it.

`blocked_reason` is deliberately separate from `drop_reason`: validation rejects a
malformed finding, a guardrail declines to publish a well-formed one. Merged into one
column, a threshold tuned too tight is indistinguishable from a bad prompt.

Revision ID: f4a1c6e2b930
Revises: e3d9a05c1782
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a1c6e2b930"
down_revision: str | None = "e3d9a05c1782"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("findings", sa.Column("github_comment_id", sa.BigInteger(), nullable=True))
    op.add_column("findings", sa.Column("blocked_reason", sa.String(length=64), nullable=True))
    # Partial index: the only query that matters is "what has already been posted on
    # this PR", which the duplicate guardrail runs on every review. Indexing the nulls
    # too would be most of the table for no reader.
    op.create_index(
        "ix_findings_posted_at",
        "findings",
        ["posted_at"],
        postgresql_where=sa.text("posted_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_findings_posted_at", table_name="findings")
    op.drop_column("findings", "blocked_reason")
    op.drop_column("findings", "github_comment_id")
    op.drop_column("findings", "posted_at")

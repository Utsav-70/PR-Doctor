"""evidence table and judgment columns

Phases 7 and 8.

`evidence` keeps every executed check, including refutations — especially refutations.
The set of refuted findings is where false-positive analysis starts, and it is how a
prompt regression is told apart from a tooling regression.

On `findings`, the verification and judgment outcomes are separate columns rather than
one status, because "validation rejected it", "evidence refuted it", "the judge declined
it" and "a guardrail blocked it" are four different diagnoses.

Revision ID: b8e3d7a15c42
Revises: f4a1c6e2b930
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e3d7a15c42"
down_revision: str | None = "f4a1c6e2b930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Phase 7 -------------------------------------------------------------
    op.add_column("findings", sa.Column("verified", sa.Boolean(), nullable=True))
    op.add_column(
        "findings", sa.Column("verified_confidence", sa.Numeric(4, 3), nullable=True)
    )

    op.create_table(
        "evidence",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("finding_id", sa.UUID(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("weight", sa.Numeric(4, 3), nullable=False, server_default="0"),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_finding_id", "evidence", ["finding_id"])
    op.create_index("ix_evidence_outcome", "evidence", ["outcome"])

    # --- Phase 8 -------------------------------------------------------------
    op.add_column("findings", sa.Column("judge_accepted", sa.Boolean(), nullable=True))
    op.add_column("findings", sa.Column("judge_confidence", sa.Numeric(4, 3), nullable=True))
    op.add_column("findings", sa.Column("judge_reason", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("rejection_code", sa.String(length=32), nullable=True))
    op.add_column("findings", sa.Column("description_original", sa.Text(), nullable=True))
    op.add_column("findings", sa.Column("dedupe_key", sa.String(length=32), nullable=True))

    op.create_index("ix_findings_judge_accepted", "findings", ["judge_accepted"])
    # The rejection-code distribution is the primary health metric of the pipeline, and
    # it is queried per repository, so it gets a real index rather than a seq scan.
    op.create_index("ix_findings_rejection_code", "findings", ["rejection_code"])
    op.create_index("ix_findings_dedupe_key", "findings", ["dedupe_key"])


def downgrade() -> None:
    op.drop_index("ix_findings_dedupe_key", table_name="findings")
    op.drop_index("ix_findings_rejection_code", table_name="findings")
    op.drop_index("ix_findings_judge_accepted", table_name="findings")
    op.drop_column("findings", "dedupe_key")
    op.drop_column("findings", "description_original")
    op.drop_column("findings", "rejection_code")
    op.drop_column("findings", "judge_reason")
    op.drop_column("findings", "judge_confidence")
    op.drop_column("findings", "judge_accepted")

    op.drop_index("ix_evidence_outcome", table_name="evidence")
    op.drop_index("ix_evidence_finding_id", table_name="evidence")
    op.drop_table("evidence")

    op.drop_column("findings", "verified_confidence")
    op.drop_column("findings", "verified")

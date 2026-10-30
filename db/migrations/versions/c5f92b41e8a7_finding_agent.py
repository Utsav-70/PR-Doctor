"""findings.agent

Phase 6. Which specialist reported a finding.

Stamped in code rather than asked of the model: the schema the provider validates against
is the wire format, and a field the model fills in with something we already know is a
field it can get wrong. Per-agent precision is the number that says whether a specialist
prompt is pulling its weight, and it cannot be reconstructed after the fact.

Revision ID: c5f92b41e8a7
Revises: b8e3d7a15c42
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5f92b41e8a7"
down_revision: str | None = "b8e3d7a15c42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("findings", sa.Column("agent", sa.String(length=16), nullable=True))
    op.create_index("ix_findings_agent", "findings", ["agent"])


def downgrade() -> None:
    op.drop_index("ix_findings_agent", table_name="findings")
    op.drop_column("findings", "agent")

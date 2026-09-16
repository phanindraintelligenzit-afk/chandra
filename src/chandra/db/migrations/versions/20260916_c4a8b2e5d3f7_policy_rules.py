"""Policy engine rule storage (PRD L2 stage 6, §26.7).

Revision ID: c4a8b2e5d3f7
Revises: b7e3c9d1f2a4
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4a8b2e5d3f7"
down_revision: str | None = "b7e3c9d1f2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_rules",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("effect", sa.String(8), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("criteria_jsonb", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", "tenant_id"),
        sa.CheckConstraint("effect in ('allow','deny')", name="ck_policy_rules_effect"),
    )
    op.create_index("ix_policy_rules_tenant_priority", "policy_rules", ["tenant_id", "priority"])


def downgrade() -> None:
    op.drop_index("ix_policy_rules_tenant_priority", table_name="policy_rules")
    op.drop_table("policy_rules")

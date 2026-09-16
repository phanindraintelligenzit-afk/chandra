"""RBAC role assignments (PRD §26.7).

Revision ID: d5b1f8c62a90
Revises: c4a8b2e5d3f7
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b1f8c62a90"
down_revision: str | None = "c4a8b2e5d3f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principal_roles",
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("granted_by", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "role"),
    )


def downgrade() -> None:
    op.drop_table("principal_roles")

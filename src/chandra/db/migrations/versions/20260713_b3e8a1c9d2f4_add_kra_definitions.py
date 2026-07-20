"""Add kra_definitions — the Dynamic KRA Framework registry table.

Revision ID: b3e8a1c9d2f4
Revises: a1d9e2f4b7c1
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "b3e8a1c9d2f4"
down_revision: str | None = "a1d9e2f4b7c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: safe to re-run against a database where a hotfix already
    # created the table (mirrors the convention set by c6f417c05ab8).
    op.execute("DROP TABLE IF EXISTS kra_definitions CASCADE")
    op.create_table(
        "kra_definitions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("keywords_jsonb", JSONB(), nullable=False, server_default="[]"),
        sa.Column("prompt_template", sa.Text(), nullable=False, server_default=""),
        sa.Column("mode", sa.String(16), nullable=False, server_default="llm"),
        sa.Column("rules_jsonb", JSONB(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("kra_definitions")

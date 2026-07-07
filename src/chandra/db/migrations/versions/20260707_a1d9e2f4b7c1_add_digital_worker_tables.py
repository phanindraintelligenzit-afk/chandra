"""Add Digital Worker tables: cloud_requests + resolution_memory

Revision ID: a1d9e2f4b7c1
Revises: c6f417c05ab8
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1d9e2f4b7c1"
down_revision: str | None = "c6f417c05ab8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloud_requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False, unique=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("priority", sa.String(4), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("decision_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("audit_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_cloud_requests_request_id", "cloud_requests", ["request_id"])
    op.create_index("ix_cloud_requests_source", "cloud_requests", ["source"])
    op.create_index("ix_cloud_requests_category", "cloud_requests", ["category"])
    op.create_index("ix_cloud_requests_platform", "cloud_requests", ["platform"])

    op.create_table(
        "resolution_memory",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("plan_jsonb", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "last_outcome", sa.String(32), nullable=False, server_default=sa.text("'unknown'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_resolution_memory_fingerprint", "resolution_memory", ["fingerprint"])
    op.create_index("ix_resolution_memory_category", "resolution_memory", ["category"])
    op.create_index("ix_resolution_memory_platform", "resolution_memory", ["platform"])


def downgrade() -> None:
    op.drop_index("ix_resolution_memory_platform", table_name="resolution_memory")
    op.drop_index("ix_resolution_memory_category", table_name="resolution_memory")
    op.drop_index("ix_resolution_memory_fingerprint", table_name="resolution_memory")
    op.drop_table("resolution_memory")
    op.drop_index("ix_cloud_requests_platform", table_name="cloud_requests")
    op.drop_index("ix_cloud_requests_category", table_name="cloud_requests")
    op.drop_index("ix_cloud_requests_source", table_name="cloud_requests")
    op.drop_index("ix_cloud_requests_request_id", table_name="cloud_requests")
    op.drop_table("cloud_requests")

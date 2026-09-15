"""Postgres as system of record for DFTE configuration; tenant + correlation ids.

Adds aws_tasks, permission_sets, custom_kras, agent_run_memory (replacing the
JSON files formerly read/written at the repo root) and adds tenant_id /
correlation_id to cloud_requests (PRD §26.8, §26.11).

Revision ID: b7e3c9d1f2a4
Revises: a1d9e2f4b7c1
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e3c9d1f2a4"
down_revision: str | None = "a1d9e2f4b7c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)
_NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.add_column(
        "cloud_requests",
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
    )
    op.add_column("cloud_requests", sa.Column("correlation_id", sa.String(64), nullable=True))
    op.create_index("ix_cloud_requests_tenant_id", "cloud_requests", ["tenant_id"])
    op.create_index("ix_cloud_requests_correlation_id", "cloud_requests", ["correlation_id"])

    op.create_table(
        "aws_tasks",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(64), nullable=False, server_default=""),
        sa.Column("ownership", sa.String(64), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_preset", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_predefined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("extra_jsonb", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id", "tenant_id"),
    )
    op.create_index("ix_aws_tasks_category", "aws_tasks", ["category"])

    op.create_table(
        "permission_sets",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("aws_service", sa.String(64), nullable=False, server_default=""),
        sa.Column("actions_jsonb", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("resource_arn", sa.Text(), nullable=False, server_default="*"),
        sa.Column("is_predefined", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("extra_jsonb", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id", "tenant_id"),
    )
    op.create_index("ix_permission_sets_aws_service", "permission_sets", ["aws_service"])

    op.create_table(
        "custom_kras",
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", _TS, server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "name"),
    )

    op.create_table(
        "agent_run_memory",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("final_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("iterations_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_jsonb", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("fixes_jsonb", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("lesson", sa.Text(), nullable=False, server_default=""),
        sa.Column("recorded_at", _TS, nullable=False),
    )
    op.create_index("ix_agent_run_memory_tenant_id", "agent_run_memory", ["tenant_id"])
    op.create_index("ix_agent_run_memory_correlation_id", "agent_run_memory", ["correlation_id"])
    op.create_index("ix_agent_run_memory_action_name", "agent_run_memory", ["action_name"])


def downgrade() -> None:
    op.drop_table("agent_run_memory")
    op.drop_table("custom_kras")
    op.drop_table("permission_sets")
    op.drop_table("aws_tasks")
    op.drop_index("ix_cloud_requests_correlation_id", table_name="cloud_requests")
    op.drop_index("ix_cloud_requests_tenant_id", table_name="cloud_requests")
    op.drop_column("cloud_requests", "correlation_id")
    op.drop_column("cloud_requests", "tenant_id")

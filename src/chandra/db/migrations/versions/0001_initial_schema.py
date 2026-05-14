"""initial schema — runs, findings, briefings, eval_runs

Revision ID: 0001
Revises:
Create Date: 2026-05-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("errors_json", JSONB(), nullable=True),
    )
    op.create_index("ix_runs_account_id", "runs", ["account_id"])

    op.create_table(
        "findings",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=False),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kra", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("detector_id", sa.String(length=64), nullable=False),
        sa.Column("resource_arn", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("evidence_jsonb", JSONB(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"])
    op.create_index("ix_findings_kra", "findings", ["kra"])
    op.create_index("ix_findings_severity", "findings", ["severity"])
    op.create_index("ix_findings_detector_id", "findings", ["detector_id"])

    op.create_table(
        "briefings",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=False),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("scorecard_jsonb", JSONB(), nullable=False),
        sa.Column("markdown_text", sa.Text(), nullable=False),
        sa.Column("findings_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    op.create_table(
        "eval_runs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=False),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("recall_overall", sa.Float(), nullable=False),
        sa.Column("recall_per_kra_jsonb", JSONB(), nullable=False),
        sa.Column("precision_overall", sa.Float(), nullable=False),
        sa.Column("fp_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("eval_runs")
    op.drop_table("briefings")
    op.drop_index("ix_findings_detector_id", table_name="findings")
    op.drop_index("ix_findings_severity", table_name="findings")
    op.drop_index("ix_findings_kra", table_name="findings")
    op.drop_index("ix_findings_run_id", table_name="findings")
    op.drop_table("findings")
    op.drop_index("ix_runs_account_id", table_name="runs")
    op.drop_table("runs")

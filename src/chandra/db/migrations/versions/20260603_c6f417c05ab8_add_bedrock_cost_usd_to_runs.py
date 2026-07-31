from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f417c05ab8"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The checkpoint_* tables only exist on databases where the LangGraph
    # PostgresSaver ran before this migration. On a fresh database (CI,
    # new environments) they are absent — use IF EXISTS so the migration
    # chain can apply from scratch. No-op for databases where the original
    # unconditional drops already succeeded.
    op.execute("DROP INDEX IF EXISTS checkpoint_writes_thread_id_idx")
    op.execute("DROP TABLE IF EXISTS checkpoint_writes")
    op.execute("DROP INDEX IF EXISTS checkpoint_blobs_thread_id_idx")
    op.execute("DROP TABLE IF EXISTS checkpoint_blobs")
    op.execute("DROP TABLE IF EXISTS checkpoint_migrations")
    op.execute("DROP INDEX IF EXISTS checkpoints_thread_id_idx")
    op.execute("DROP TABLE IF EXISTS checkpoints")
    op.add_column(
        "runs",
        sa.Column("bedrock_cost_usd", sa.Float(), server_default=sa.text("0.0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runs", "bedrock_cost_usd")
    op.create_table(
        "checkpoints",
        sa.Column("thread_id", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column(
            "checkpoint_ns",
            sa.TEXT(),
            server_default=sa.text("''::text"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("checkpoint_id", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("parent_checkpoint_id", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("type", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("checkpoint", sa.LargeBinary(), autoincrement=False, nullable=True),
        sa.Column("metadata_", sa.LargeBinary(), autoincrement=False, nullable=True),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
    )
    op.create_index("checkpoints_thread_id_idx", "checkpoints", ["thread_id"])
    op.create_table(
        "checkpoint_migrations",
        sa.Column("v", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("v"),
    )
    op.create_table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("checkpoint_ns", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("channel", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("version", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("type", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("blob", sa.LargeBinary(), autoincrement=False, nullable=True),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "channel", "version"),
    )
    op.create_index("checkpoint_blobs_thread_id_idx", "checkpoint_blobs", ["thread_id"])
    op.create_table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("checkpoint_ns", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("checkpoint_id", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("task_id", sa.TEXT(), autoincrement=False, nullable=False),
        sa.Column("idx", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("channel", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("type", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("value", sa.LargeBinary(), autoincrement=False, nullable=True),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    )
    op.create_index("checkpoint_writes_thread_id_idx", "checkpoint_writes", ["thread_id"])

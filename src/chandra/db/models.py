"""SQLAlchemy ORM models for Chandra's relational state.

Schema is defined here once; alembic migrations diff against this metadata.
Per the master prompt's anti-pattern list: NOTHING outside the ``persist``
node and migrations is allowed to write to these tables.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base. JSONB on Postgres, JSON elsewhere (for sqlite tests)."""

    type_annotation_map = {
        dict: JSONB().with_variant(JSON(), "sqlite"),
        list: JSONB().with_variant(JSON(), "sqlite"),
    }


def _uuid() -> str:
    return str(uuid4())


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts datetime objects to ISO format strings."""
    
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()  # Convert datetime to ISO 8601 string
        return super().default(obj)


def serialize_finding_evidence(evidence_dict: dict) -> dict:
    """
    Convert all datetime objects in evidence_jsonb to ISO strings.
    This prevents 'datetime is not JSON serializable' errors.
    """
    if not evidence_dict:
        return evidence_dict
    
    # Use custom encoder to convert to JSON string, then back to dict
    # This ensures all datetime objects are converted to strings
    json_str = json.dumps(evidence_dict, cls=DateTimeEncoder)
    return json.loads(json_str)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=_uuid,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    errors_json: Mapped[list | None] = mapped_column("errors_json")
    bedrock_cost_usd: Mapped[float] = mapped_column(server_default=text("0.0"), default=0.0)

    findings: Mapped[list["Finding"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    briefing: Mapped["Briefing | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    eval_run: Mapped["EvalRun | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=_uuid,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kra: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    detector_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_arn: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_jsonb: Mapped[dict] = mapped_column("evidence_jsonb", nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="findings")


class Briefing(Base):
    __tablename__ = "briefings"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=_uuid,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    scorecard_jsonb: Mapped[dict] = mapped_column("scorecard_jsonb", nullable=False)
    markdown_text: Mapped[str] = mapped_column(Text, nullable=False)
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="briefing")


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=_uuid,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    recall_overall: Mapped[float] = mapped_column(nullable=False)
    recall_per_kra_jsonb: Mapped[dict] = mapped_column("recall_per_kra_jsonb", nullable=False)
    precision_overall: Mapped[float] = mapped_column(nullable=False)
    fp_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="eval_run")
"""SQLAlchemy ORM models for Chandra's relational state.

Schema is defined here once; alembic migrations diff against this metadata.
Per the master prompt's anti-pattern list: NOTHING outside the ``persist``
node and migrations is allowed to write to these tables.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar
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

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict: JSONB().with_variant(JSON(), "sqlite"),
        dict[str, Any]: JSONB().with_variant(JSON(), "sqlite"),
        list: JSONB().with_variant(JSON(), "sqlite"),
        list[Any]: JSONB().with_variant(JSON(), "sqlite"),
    }


def _uuid() -> str:
    return str(uuid4())


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder that converts datetime objects to ISO format strings."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()  # Convert datetime to ISO 8601 string
        return super().default(obj)


def serialize_finding_evidence(evidence_dict: dict[str, Any]) -> dict[str, Any]:
    """
    Convert all datetime objects in evidence_jsonb to ISO strings.
    This prevents 'datetime is not JSON serializable' errors.
    """
    if not evidence_dict:
        return evidence_dict

    # Use custom encoder to convert to JSON string, then back to dict
    # This ensures all datetime objects are converted to strings
    json_str = json.dumps(evidence_dict, cls=DateTimeEncoder)
    result: dict[str, Any] = json.loads(json_str)
    return result


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
    errors_json: Mapped[list[Any] | None] = mapped_column("errors_json")
    bedrock_cost_usd: Mapped[float] = mapped_column(server_default=text("0.0"), default=0.0)

    findings: Mapped[list[Finding]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    briefing: Mapped[Briefing | None] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    eval_run: Mapped[EvalRun | None] = relationship(
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
    evidence_jsonb: Mapped[dict[str, Any]] = mapped_column("evidence_jsonb", nullable=False)
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
    scorecard_jsonb: Mapped[dict[str, Any]] = mapped_column("scorecard_jsonb", nullable=False)
    markdown_text: Mapped[str] = mapped_column(Text, nullable=False)
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="briefing")


class CloudRequestRecord(Base):
    """Audit record for one Digital Worker request workflow (any channel)."""

    __tablename__ = "cloud_requests"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=_uuid,
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(4), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    result_jsonb: Mapped[dict[str, Any]] = mapped_column("result_jsonb", nullable=False)
    audit_jsonb: Mapped[list[Any]] = mapped_column("audit_jsonb", nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )


class ResolutionMemoryRecord(Base):
    """Past-history execution steps cache (Proposedflow 'System Memory').

    Keyed by a stable fingerprint of the classified request so recurring
    problems reuse previously generated steps instead of a fresh LLM call.
    """

    __tablename__ = "resolution_memory"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False).with_variant(String(36), "sqlite"),
        primary_key=True,
        default=_uuid,
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    plan_jsonb: Mapped[dict[str, Any]] = mapped_column("plan_jsonb", nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    recall_per_kra_jsonb: Mapped[dict[str, Any]] = mapped_column(
        "recall_per_kra_jsonb", nullable=False
    )
    precision_overall: Mapped[float] = mapped_column(nullable=False)
    fp_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="eval_run")

"""Pydantic schemas shared between tools, graph nodes, and briefing output."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KRA = Literal["cost", "security", "compliance", "performance", "reliability"]
Severity = Literal["critical", "high", "medium", "low", "info"]

SEVERITY_WEIGHTS: dict[str, int] = {
    "critical": 10,
    "high": 5,
    "medium": 2,
    "low": 1,
    "info": 0,
}

KRAS: tuple[str, ...] = ("cost", "security", "compliance", "performance", "reliability")


class Finding(BaseModel):
    """A single detection emitted by a deterministic tool.

    The LLM is never allowed to invent these — it only ranks and explains
    findings produced by :mod:`chandra.tools`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kra: KRA
    severity: Severity
    resource_arn: str = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    region: str = Field(min_length=1)
    title: str = Field(min_length=1)
    evidence: dict = Field(default_factory=dict)
    recommendation: str = Field(min_length=1)
    detector_id: str = Field(min_length=1)


class AnalyzedFinding(BaseModel):
    """Finding enriched with LLM ranking + dedup metadata."""

    model_config = ConfigDict(extra="forbid")

    finding: Finding
    rank: int = Field(ge=0)
    rationale: str = ""
    deduped_from: list[str] = Field(default_factory=list)


class Scorecard(BaseModel):
    """Per-KRA score (0..100) and overall mean."""

    model_config = ConfigDict(extra="forbid")

    cost: int = Field(ge=0, le=100)
    security: int = Field(ge=0, le=100)
    compliance: int = Field(ge=0, le=100)
    performance: int = Field(ge=0, le=100)
    reliability: int = Field(ge=0, le=100)
    overall: int = Field(ge=0, le=100)

    def as_dict(self) -> dict[str, int]:
        return self.model_dump()


class BriefingPayload(BaseModel):
    """Structured form of the briefing — mirror of the rendered markdown."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    account_id: str
    generated_at: datetime
    scorecard: Scorecard
    executive_summary: list[str]
    top_findings: list[AnalyzedFinding]
    all_findings: list[Finding]
    metadata: dict

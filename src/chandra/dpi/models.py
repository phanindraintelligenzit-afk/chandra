"""
DPI-LS Pydantic Models
Digital FTE Performance Index — data structures
"""

from datetime import datetime
from typing import Dict, Optional
from pydantic import BaseModel, Field, validator


class DPIDimension(BaseModel):
    """
    7-dimension scoring input for a Digital Worker.
    All values are 0-100 floats.
    """

    productivity: float = Field(
        ...,
        ge=0,
        le=100,
        description="P — Tasks completed vs tasks assigned (%)",
    )
    quality: float = Field(
        ...,
        ge=0,
        le=100,
        description="Q — Accuracy / no rework required (%)",
    )
    execution: float = Field(
        ...,
        ge=0,
        le=100,
        description="E — Autonomous completion rate (%)",
    )
    governance: float = Field(
        ...,
        ge=0,
        le=100,
        description="G — Policy compliance (%)",
    )
    risk: float = Field(
        ...,
        ge=0,
        le=100,
        description="R — 100 minus incidents caused (%)",
    )
    validation: float = Field(
        ...,
        ge=0,
        le=100,
        description="V — Audit pass rate (%)",
    )
    cost: float = Field(
        ...,
        ge=0,
        le=100,
        description="C — Cost efficiency score (%)",
    )


class DPIResult(BaseModel):
    """
    Scored output from the DPI-LS engine.
    """

    worker_id: Optional[str] = Field(None, description="Worker identifier")
    worker_name: Optional[str] = Field(None, description="Worker display name")
    worker_role: Optional[str] = Field(None, description="Worker role (e.g. AWS Cloud Engineer)")
    maturity_level: Optional[str] = Field(None, description="Maturity level (L1-L4)")

    dimensions: DPIDimension
    weighted_scores: Dict[str, float] = Field(
        ..., description="Each dimension's weighted contribution"
    )
    overall_score: float = Field(..., description="Final weighted score (0-100)")
    rating: str = Field(
        ...,
        description="Rating label: Exceptional / Strong / Developing / Needs Improvement",
    )
    rating_color: str = Field(..., description="Hex color for the rating badge")
    rating_icon: str = Field(..., description="Emoji icon for the rating")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Human-readable breakdown
    strengths: list[str] = Field(default_factory=list)
    improvement_areas: list[str] = Field(default_factory=list)
    recommendation: str = Field("", description="One-line recommendation")


class DPIScoreRequest(BaseModel):
    """Request body for POST /api/dpi/score"""

    worker_id: Optional[str] = None
    worker_name: Optional[str] = None
    worker_role: Optional[str] = None
    maturity_level: Optional[str] = None
    dimensions: DPIDimension


class DPILeaderboardEntry(BaseModel):
    """Single entry in the leaderboard response"""

    worker_id: str
    worker_name: str
    worker_role: Optional[str] = None
    maturity_level: Optional[str] = None
    overall_score: float
    rating: str
    rating_color: str
    rating_icon: str
    timestamp: datetime

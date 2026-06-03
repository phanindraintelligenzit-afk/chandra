"""
DPI-LS Scoring Engine
Digital FTE Performance Index — core computation logic

Weights (must sum to 1.0):
  P - Productivity    20%  (most important: are they actually doing work?)
  Q - Quality         15%  (is the work correct?)
  E - Execution       20%  (can they act autonomously without human help?)
  G - Governance      15%  (are they compliant with policy?)
  R - Risk            10%  (are they causing incidents?)
  V - Validation      10%  (do they pass audits?)
  C - Cost            10%  (are they cost efficient?)

Rating Thresholds:
  >= 90  → Exceptional Digital Worker
  >= 75  → Strong Digital Worker
  >= 60  → Developing Digital Worker
  <  60  → Needs Improvement
"""

from datetime import datetime
from typing import Optional
from .models import DPIDimension, DPIResult, DPIScoreRequest

# Dimension weights — must sum to 1.0
WEIGHTS = {
    "productivity": 0.20,
    "quality": 0.15,
    "execution": 0.20,
    "governance": 0.15,
    "risk": 0.10,
    "validation": 0.10,
    "cost": 0.10,
}

# Rating thresholds
RATING_THRESHOLDS = [
    (90, "Exceptional Digital Worker", "#10B981", "🏆"),
    (75, "Strong Digital Worker", "#3B82F6", "⭐"),
    (60, "Developing Digital Worker", "#F59E0B", "📈"),
    (0, "Needs Improvement", "#EF4444", "⚠️"),
]

# Dimension display names
DIMENSION_LABELS = {
    "productivity": "Productivity",
    "quality": "Quality",
    "execution": "Execution",
    "governance": "Governance",
    "risk": "Risk Control",
    "validation": "Validation",
    "cost": "Cost Efficiency",
}

# Strength / improvement thresholds
STRENGTH_THRESHOLD = 85.0
IMPROVEMENT_THRESHOLD = 70.0


def get_rating(score: float) -> tuple[str, str, str]:
    """
    Return (rating_label, hex_color, emoji_icon) for a given score.
    """
    for threshold, label, color, icon in RATING_THRESHOLDS:
        if score >= threshold:
            return label, color, icon
    return "Needs Improvement", "#EF4444", "⚠️"


def score_worker(
    dimensions: DPIDimension,
    worker_id: Optional[str] = None,
    worker_name: Optional[str] = None,
    worker_role: Optional[str] = None,
    maturity_level: Optional[str] = None,
) -> DPIResult:
    """
    Compute a full DPI-LS score for a Digital Worker.

    Args:
        dimensions: The 7-dimension input scores (all 0–100)
        worker_id:  Optional unique identifier
        worker_name: Optional display name
        worker_role: Optional role label
        maturity_level: Optional L1–L4 maturity level

    Returns:
        DPIResult with weighted score, rating, strengths, and improvement areas
    """
    dim_dict = dimensions.model_dump()

    # Compute weighted contribution for each dimension
    weighted_scores = {
        key: round(dim_dict[key] * WEIGHTS[key], 4) for key in WEIGHTS
    }

    # Overall score = sum of all weighted contributions × 100 / sum_of_weights
    # (weights already sum to 1.0, so this is simply the sum)
    overall = round(sum(weighted_scores.values()), 2)

    # Get rating
    rating, color, icon = get_rating(overall)

    # Identify strengths and improvement areas
    strengths = [
        f"{DIMENSION_LABELS[k]} ({dim_dict[k]:.0f}/100)"
        for k in dim_dict
        if dim_dict[k] >= STRENGTH_THRESHOLD
    ]

    improvement_areas = [
        f"{DIMENSION_LABELS[k]} ({dim_dict[k]:.0f}/100)"
        for k in dim_dict
        if dim_dict[k] < IMPROVEMENT_THRESHOLD
    ]

    # Generate recommendation
    if overall >= 90:
        recommendation = "This worker is performing at an elite level. Consider promoting to L4 tasks."
    elif overall >= 75:
        recommendation = "Strong performer. Focus on improving Execution autonomy to reach Exceptional."
    elif overall >= 60:
        recommendation = f"Developing worker. Priority improvement areas: {', '.join(improvement_areas[:2]) if improvement_areas else 'Review all dimensions'}."
    else:
        recommendation = "Significant gaps detected. Worker needs structured improvement plan and closer supervision."

    return DPIResult(
        worker_id=worker_id,
        worker_name=worker_name,
        worker_role=worker_role,
        maturity_level=maturity_level,
        dimensions=dimensions,
        weighted_scores=weighted_scores,
        overall_score=overall,
        rating=rating,
        rating_color=color,
        rating_icon=icon,
        timestamp=datetime.utcnow(),
        strengths=strengths,
        improvement_areas=improvement_areas,
        recommendation=recommendation,
    )


def score_from_request(request: DPIScoreRequest) -> DPIResult:
    """Convenience wrapper for FastAPI route handlers."""
    return score_worker(
        dimensions=request.dimensions,
        worker_id=request.worker_id,
        worker_name=request.worker_name,
        worker_role=request.worker_role,
        maturity_level=request.maturity_level,
    )

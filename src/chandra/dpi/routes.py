"""
DPI-LS FastAPI Routes
Digital FTE Performance Index — API endpoints

Endpoints:
  POST /api/dpi/score          → Score any Digital Worker in real time
  GET  /api/dpi/demo           → Demo score with preset example data
  GET  /api/dpi/leaderboard    → Return top N workers (in-memory store)
  DELETE /api/dpi/leaderboard  → Reset leaderboard (dev/test use)
"""

from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query

from .engine import score_from_request, score_worker
from .models import (
    DPIDimension,
    DPILeaderboardEntry,
    DPIResult,
    DPIScoreRequest,
)

router = APIRouter()

# In-memory leaderboard store (keyed by worker_id)
# In production, replace with DB persistence
_leaderboard: dict[str, DPILeaderboardEntry] = {}


@router.post("/score", response_model=DPIResult, summary="Score a Digital Worker")
async def score_digital_worker(request: DPIScoreRequest) -> DPIResult:
    """
    Score any Digital Worker across 7 dimensions:
    - P: Productivity (0-100)
    - Q: Quality (0-100)
    - E: Execution autonomy (0-100)
    - G: Governance/compliance (0-100)
    - R: Risk control (0-100)
    - V: Validation/audit (0-100)
    - C: Cost efficiency (0-100)

    Returns a weighted overall score with rating and improvement insights.
    """
    result = score_from_request(request)

    # Upsert into leaderboard if worker_id provided
    if result.worker_id:
        _leaderboard[result.worker_id] = DPILeaderboardEntry(
            worker_id=result.worker_id,
            worker_name=result.worker_name or result.worker_id,
            worker_role=result.worker_role,
            maturity_level=result.maturity_level,
            overall_score=result.overall_score,
            rating=result.rating,
            rating_color=result.rating_color,
            rating_icon=result.rating_icon,
            timestamp=result.timestamp,
        )

    return result


@router.get("/demo", response_model=DPIResult, summary="Demo DPI score")
async def demo_dpi_score() -> DPIResult:
    """
    Returns a pre-populated demo score for an AWS Cloud Engineer worker.
    Useful for UI prototyping and demos.
    """
    return score_worker(
        dimensions=DPIDimension(
            productivity=90,
            quality=88,
            execution=82,
            governance=95,
            risk=90,
            validation=87,
            cost=80,
        ),
        worker_id="demo-worker-001",
        worker_name="Arjun — AWS Cloud Engineer",
        worker_role="AWS Cloud Engineer",
        maturity_level="L3",
    )


@router.get(
    "/leaderboard",
    response_model=List[DPILeaderboardEntry],
    summary="Digital Worker leaderboard",
)
async def get_leaderboard(
    top_n: int = Query(10, ge=1, le=100, description="Return top N workers by score"),
) -> List[DPILeaderboardEntry]:
    """
    Returns the top N Digital Workers ranked by overall DPI-LS score.
    Workers are scored via POST /api/dpi/score and stored in memory.
    """
    sorted_workers = sorted(
        _leaderboard.values(),
        key=lambda w: w.overall_score,
        reverse=True,
    )
    return sorted_workers[:top_n]


@router.delete(
    "/leaderboard",
    summary="Reset leaderboard (dev/test only)",
)
async def reset_leaderboard() -> dict:
    """Clears the in-memory leaderboard. For development and testing only."""
    _leaderboard.clear()
    return {"message": "Leaderboard cleared", "timestamp": datetime.utcnow().isoformat()}

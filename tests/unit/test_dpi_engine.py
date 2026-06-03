"""Unit tests for the DPI-LS performance appraisal engine."""

import pytest
from src.chandra.dpi.engine import calculate_dpi_score


def test_dpi_perfect_score() -> None:
    """Perfect inputs should yield exactly 100 overall score."""
    metrics = {
        "productivity": 1.0,
        "quality": 1.0,
        "execution": 1.0,
        "governance": 1.0,
        "risk": 1.0,
        "validation": 1.0,
        "cost": 1.0,
    }
    result = calculate_dpi_score(metrics)
    assert result["score_final"] == 100.0
    assert result["score_raw"] == 100.0
    assert result["gate_breached"] is False
    assert result["rating_band"] == "Exceptional"


def test_dpi_strong_score() -> None:
    """Inputs of 0.92 should yield exactly 92 overall score."""
    metrics = {
        "productivity": 0.92,
        "quality": 0.92,
        "execution": 0.92,
        "governance": 0.92,
        "risk": 0.92,
        "validation": 0.92,
        "cost": 0.92,
    }
    result = calculate_dpi_score(metrics)
    assert result["score_final"] == 92.0
    assert result["score_raw"] == 92.0
    assert result["gate_breached"] is False
    assert result["rating_band"] == "Exceptional"


def test_dpi_mediocre_score() -> None:
    """Inputs of 0.65 should yield exactly 65 overall score (geometric mean of same value is value)."""
    metrics = {
        "productivity": 0.65,
        "quality": 0.65,
        "execution": 0.65,
        "governance": 0.65,
        "risk": 0.65,
        "validation": 0.65,
        "cost": 0.65,
    }
    result = calculate_dpi_score(metrics)
    assert result["score_final"] == 65.0
    assert result["gate_breached"] is False
    assert result["rating_band"] == "Needs Optimization"


def test_dpi_governance_gate_breach() -> None:
    """A governance score below 0.60 must trigger the compliance gate and cap the score at 69."""
    metrics = {
        "productivity": 0.95,
        "quality": 0.95,
        "execution": 0.95,
        "governance": 0.25,  # G_floor is 0.60
        "risk": 0.95,
        "validation": 0.95,
        "cost": 0.95,
    }
    result = calculate_dpi_score(metrics)
    assert result["score_raw"] > 70.0  # raw score should be high
    assert result["score_final"] == 69.0  # capped at 69
    assert result["gate_breached"] is True
    assert result["violations"]["governance"] is True
    assert "Gate Breach" in result["rating_band"]


def test_dpi_raw_metrics_conversion() -> None:
    """Verify that raw metrics (dicts) are correctly converted to 0-1 values."""
    metrics = {
        "productivity": {"ai_output": 8, "human_baseline": 10},  # P = 0.8
        "quality": {"accuracy": 0.9, "consistency": 0.8, "hallucination": 0.1},  # Q = 0.7*0.9 + 0.2*0.8 + 0.1*0.9 = 0.63 + 0.16 + 0.09 = 0.88
        "execution": {"successful_executions": 9, "total_attempts": 10},  # E = 0.9
        "governance": {"policy_violations": 1, "total_actions": 10},  # G = 0.9
        "risk": {
            "incidents": [
                {"severity": "critical", "count": 1},  # 1.0
                {"severity": "high", "count": 2},  # 1.0
                {"severity": "medium", "count": 1},  # 0.2
            ],  # total = 2.2
            "r_max": 10.0,
        },  # R = 1 - 2.2/10 = 0.78
        "validation": {"validated_components": 9, "total_required": 10},  # V = 0.9
        "cost": {"human_cost": 50, "ai_cost": 100, "utilization": 0.8},  # C = min(1, 0.5) * 0.8 = 0.4
    }
    result = calculate_dpi_score(metrics)
    assert result["metrics"]["productivity"] == pytest.approx(0.8)
    assert result["metrics"]["quality"] == pytest.approx(0.88)
    assert result["metrics"]["execution"] == pytest.approx(0.9)
    assert result["metrics"]["governance"] == pytest.approx(0.9)
    assert result["metrics"]["risk"] == pytest.approx(0.78)
    assert result["metrics"]["validation"] == pytest.approx(0.9)
    assert result["metrics"]["cost"] == pytest.approx(0.4)
    assert result["gate_breached"] is False

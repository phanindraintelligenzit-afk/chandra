"""Unit tests for decision router (low-risk auto-fix vs high-risk escalate)."""

from __future__ import annotations

from src.chandra.briefing.schemas import AnalyzedFinding, Finding, Severity
from src.chandra.graphs.nodes import decision_router
from src.chandra.graphs.state import ChandraState


def _af(severity: Severity, detector_id: str = "TEST-001") -> AnalyzedFinding:
    """Helper: create an AnalyzedFinding with given severity."""
    return AnalyzedFinding(
        finding=Finding(
            kra="security",  # type: ignore[arg-type]
            severity=severity,
            resource_arn=f"arn:aws:s3:::bucket-{detector_id}",
            resource_type="AWS::S3::Bucket",
            region="us-east-1",
            title=f"Test finding {severity}",
            evidence={},
            recommendation="Fix this issue",
            detector_id=detector_id,
        ),
        rank=0,
        rationale="This is a test finding",
    )


class TestDecisionRouter:
    def test_critical_escalated(self) -> None:
        """Critical finding escalates to pending_writes."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[_af("critical", "CRIT-001")],  # type: ignore[arg-type]
        )
        result = decision_router(state)

        assert len(result["pending_writes"]) == 1
        assert len(result["auto_fixed"]) == 0
        assert result["pending_writes"][0].risk_level == "high"
        assert result["pending_writes"][0].target_arn == "arn:aws:s3:::bucket-CRIT-001"
        assert result["pending_writes"][0].action == "remediate_CRIT-001"

    def test_high_escalated(self) -> None:
        """High finding escalates to pending_writes."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[_af("high", "HIGH-001")],  # type: ignore[arg-type]
        )
        result = decision_router(state)

        assert len(result["pending_writes"]) == 1
        assert len(result["auto_fixed"]) == 0
        assert result["pending_writes"][0].risk_level == "high"

    def test_medium_auto_fixed(self) -> None:
        """Medium finding goes to auto_fixed."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[_af("medium", "MED-001")],  # type: ignore[arg-type]
        )
        result = decision_router(state)

        assert len(result["pending_writes"]) == 0
        assert len(result["auto_fixed"]) == 1
        assert result["auto_fixed"][0].risk_level == "low"
        assert result["auto_fixed"][0].target_arn == "arn:aws:s3:::bucket-MED-001"

    def test_low_auto_fixed(self) -> None:
        """Low finding goes to auto_fixed."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[_af("low", "LOW-001")],  # type: ignore[arg-type]
        )
        result = decision_router(state)

        assert len(result["pending_writes"]) == 0
        assert len(result["auto_fixed"]) == 1
        assert result["auto_fixed"][0].risk_level == "low"

    def test_info_auto_fixed(self) -> None:
        """Info finding goes to auto_fixed."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[_af("info", "INFO-001")],  # type: ignore[arg-type]
        )
        result = decision_router(state)

        assert len(result["pending_writes"]) == 0
        assert len(result["auto_fixed"]) == 1
        assert result["auto_fixed"][0].risk_level == "low"

    def test_empty_findings(self) -> None:
        """No findings produces empty lists."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[],
        )
        result = decision_router(state)

        assert result["pending_writes"] == []
        assert result["auto_fixed"] == []

    def test_mixed_findings(self) -> None:
        """Mixed severities split correctly."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[
                _af("critical", "CRIT-001"),  # type: ignore[arg-type]
                _af("high", "HIGH-001"),  # type: ignore[arg-type]
                _af("medium", "MED-001"),  # type: ignore[arg-type]
                _af("low", "LOW-001"),  # type: ignore[arg-type]
                _af("info", "INFO-001"),  # type: ignore[arg-type]
            ],
        )
        result = decision_router(state)

        # Critical + high = 2 escalations
        assert len(result["pending_writes"]) == 2
        assert {w.action for w in result["pending_writes"]} == {
            "remediate_CRIT-001",
            "remediate_HIGH-001",
        }
        # Medium + low + info = 3 auto-fixes
        assert len(result["auto_fixed"]) == 3
        assert {w.action for w in result["auto_fixed"]} == {
            "remediate_MED-001",
            "remediate_LOW-001",
            "remediate_INFO-001",
        }

    def test_all_escalations_have_high_risk(self) -> None:
        """All pending_writes have risk_level='high'."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[
                _af("critical", "C1"),  # type: ignore[arg-type]
                _af("high", "H1"),  # type: ignore[arg-type]
            ],
        )
        result = decision_router(state)

        assert all(w.risk_level == "high" for w in result["pending_writes"])

    def test_all_auto_fixes_have_low_risk(self) -> None:
        """All auto_fixed have risk_level='low'."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[
                _af("medium", "M1"),  # type: ignore[arg-type]
                _af("low", "L1"),  # type: ignore[arg-type]
                _af("info", "I1"),  # type: ignore[arg-type]
            ],
        )
        result = decision_router(state)

        assert all(w.risk_level == "low" for w in result["auto_fixed"])

    def test_proposed_writes_have_correct_fields(self) -> None:
        """ProposedWrite records are fully populated."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            analyzed_findings=[_af("high", "TEST-001")],  # type: ignore[arg-type]
        )
        result = decision_router(state)

        write = result["pending_writes"][0]
        assert write.action == "remediate_TEST-001"
        assert write.target_arn == "arn:aws:s3:::bucket-TEST-001"
        assert write.requested_by == "decision_router"
        assert "Fix this issue" in write.justification or "test finding" in write.justification
        assert write.risk_level == "high"

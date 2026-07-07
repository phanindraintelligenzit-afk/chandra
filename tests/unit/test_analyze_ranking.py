"""Unit tests for ``chandra.graphs.nodes.analyze`` — LLM ranking with fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.chandra.briefing.schemas import Finding
from src.chandra.graphs.action_nodes import analyze
from src.chandra.graphs.state import ChandraState


def _finding(*, kra: str, severity: str, detector_id: str, arn_suffix: str = "x") -> Finding:
    return Finding(
        kra=kra,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        resource_arn=f"arn:aws:test:::{arn_suffix}",
        resource_type="Test",
        region="us-east-1",
        title=f"finding {detector_id}",
        evidence={"k": "v"},
        recommendation="fix it",
        detector_id=detector_id,
    )


class TestAnalyzeWithLlmRank:
    def test_llm_rank_returns_rationales(self) -> None:
        """When LLM succeeds, AnalyzedFinding contains rationales."""
        findings = [
            _finding(kra="security", severity="critical", detector_id="C1", arn_suffix="1"),
            _finding(kra="security", severity="high", detector_id="H1", arn_suffix="2"),
        ]
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            raw_findings={"security": findings},
        )

        # Mock ChatBedrockConverse to return ranked findings with rationales.
        mock_response = MagicMock()
        mock_response.content = (
            '{"ranked": [{"detector_id": "H1", "resource_arn": "arn:aws:test:::2", '
            '"rationale": "This issue is actually more urgent because it affects public '
            'access"}, {"detector_id": "C1", "resource_arn": "arn:aws:test:::1", '
            '"rationale": "Critical but already mitigated by WAF"}]}'
        )

        with patch("langchain_aws.ChatBedrockConverse") as mock_bedrock:
            mock_instance = MagicMock()
            mock_instance.invoke.return_value = mock_response
            mock_bedrock.return_value = mock_instance

            result = analyze(state)

        analyzed = result["analyzed_findings"]
        # LLM ranked H1 first, so it should be at rank 0
        assert analyzed[0].finding.detector_id == "H1"
        assert analyzed[0].rank == 0
        assert "urgent" in analyzed[0].rationale.lower()

        # C1 should be ranked after, with a rationale
        assert analyzed[1].finding.detector_id == "C1"
        assert analyzed[1].rank == 1
        assert "mitigated" in analyzed[1].rationale.lower()

    def test_llm_rank_falls_back_to_deterministic_on_error(self) -> None:
        """When LLM fails, analyze falls back to deterministic ranking."""
        findings = [
            _finding(kra="security", severity="critical", detector_id="C1", arn_suffix="1"),
            _finding(kra="security", severity="high", detector_id="H1", arn_suffix="2"),
        ]
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            raw_findings={"security": findings},
        )

        with patch("langchain_aws.ChatBedrockConverse") as mock_bedrock:
            mock_instance = MagicMock()
            mock_instance.invoke.side_effect = Exception("Bedrock unavailable")
            mock_bedrock.return_value = mock_instance

            result = analyze(state)

        analyzed = result["analyzed_findings"]
        # Should fall back to deterministic: critical (C1) outranks high (H1)
        assert analyzed[0].finding.detector_id == "C1"
        assert analyzed[0].rank == 0
        # Deterministic fallback should have empty rationale
        assert analyzed[0].rationale == ""

        assert analyzed[1].finding.detector_id == "H1"
        assert analyzed[1].rank == 1
        assert analyzed[1].rationale == ""

    def test_empty_findings_list(self) -> None:
        """Analyze handles empty findings list without error."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            raw_findings={},
        )

        result = analyze(state)

        assert result["analyzed_findings"] == []
        assert result["scorecard"]["overall"] == 100

    def test_multi_kra_findings(self) -> None:
        """Analyze processes findings across multiple KRAs."""
        findings = {
            "security": [
                _finding(kra="security", severity="critical", detector_id="SEC-1", arn_suffix="s1"),
            ],
            "cost": [
                _finding(kra="cost", severity="high", detector_id="COST-1", arn_suffix="c1"),
            ],
        }
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            raw_findings=findings,
        )

        # Mock LLM to return security finding first (custom ranking)
        mock_response = MagicMock()
        mock_response.content = (
            '{"ranked": [{"detector_id": "SEC-1", "resource_arn": "arn:aws:test:::s1", '
            '"rationale": "Security takes precedence"}, {"detector_id": "COST-1", '
            '"resource_arn": "arn:aws:test:::c1", "rationale": "Cost can wait"}]}'
        )

        with patch("langchain_aws.ChatBedrockConverse") as mock_bedrock:
            mock_instance = MagicMock()
            mock_instance.invoke.return_value = mock_response
            mock_bedrock.return_value = mock_instance

            result = analyze(state)

        analyzed = result["analyzed_findings"]
        assert len(analyzed) == 2
        assert analyzed[0].finding.detector_id == "SEC-1"
        assert analyzed[1].finding.detector_id == "COST-1"
        assert result["scorecard"]["security"] == 50  # 1 critical
        assert result["scorecard"]["cost"] == 75  # 1 high

    def test_llm_partial_response_appends_missing_findings(self) -> None:
        """If LLM omits findings, they're appended at end with empty rationale."""
        findings = [
            _finding(kra="security", severity="critical", detector_id="C1", arn_suffix="1"),
            _finding(kra="security", severity="high", detector_id="H1", arn_suffix="2"),
        ]
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            raw_findings={"security": findings},
        )

        # LLM only returns one finding (dropping H1)
        mock_response = MagicMock()
        mock_response.content = (
            '{"ranked": [{"detector_id": "C1", "resource_arn": "arn:aws:test:::1", '
            '"rationale": "Critical issue"}]}'
        )

        with patch("langchain_aws.ChatBedrockConverse") as mock_bedrock:
            mock_instance = MagicMock()
            mock_instance.invoke.return_value = mock_response
            mock_bedrock.return_value = mock_instance

            result = analyze(state)

        analyzed = result["analyzed_findings"]
        # Both findings should be present
        assert len(analyzed) == 2
        assert analyzed[0].finding.detector_id == "C1"
        assert analyzed[0].rationale == "Critical issue"
        # Missing finding appended at end with empty rationale
        assert analyzed[1].finding.detector_id == "H1"
        assert analyzed[1].rationale == ""

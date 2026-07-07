"""Unit tests for ``chandra.briefing.composer`` — math and rendering only.

LLM paths are exercised via monkeypatch fallbacks; integration tests cover
the real Bedrock round-trip when credentials are available.
"""

from __future__ import annotations

from src.chandra.briefing import composer
from src.chandra.briefing.schemas import Finding, Scorecard


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


class TestScorecard:
    def test_clean_account_scores_100(self) -> None:
        sc = composer.score_findings({})
        assert sc.overall == 100
        assert sc.security == 100

    def test_one_critical_per_kra(self) -> None:
        raw = {
            kra: [_finding(kra=kra, severity="critical", detector_id=f"{kra}-1")]
            for kra in ("cost", "security", "compliance", "performance", "reliability")
        }
        sc = composer.score_findings(raw)
        # one critical = penalty 10 * 5 = 50, score = 50
        assert sc.security == 50
        assert sc.overall == 50

    def test_score_floors_at_zero(self) -> None:
        raw = {
            "security": [
                _finding(
                    kra="security", severity="critical", detector_id=f"s{i}", arn_suffix=str(i)
                )
                for i in range(50)
            ]
        }
        sc = composer.score_findings(raw)
        assert sc.security == 0


class TestDeterministicRank:
    def test_critical_outranks_low(self) -> None:
        items = [
            _finding(kra="cost", severity="low", detector_id="L1"),
            _finding(kra="security", severity="critical", detector_id="C1"),
        ]
        ranked = composer.deterministic_rank(items)
        assert ranked[0].finding.severity == "critical"
        assert ranked[0].rank == 0
        assert ranked[1].finding.severity == "low"

    def test_kra_tiebreak_security_first(self) -> None:
        items = [
            _finding(kra="cost", severity="high", detector_id="H-cost"),
            _finding(kra="security", severity="high", detector_id="H-sec"),
        ]
        ranked = composer.deterministic_rank(items)
        assert ranked[0].finding.kra == "security"


class TestRender:
    def test_markdown_contains_required_sections(self) -> None:
        scorecard = Scorecard(
            cost=80,
            security=70,
            compliance=60,
            performance=90,
            reliability=85,
            overall=77,
        )
        findings = [_finding(kra="security", severity="critical", detector_id="C1")]
        analyzed = composer.deterministic_rank(findings)
        md, payload = composer.render_markdown(
            run_id="r1",
            account_id="123456789012",
            scorecard=scorecard.as_dict(),
            executive_summary=["a", "b", "c"],
            top_findings=analyzed,
            all_findings=findings,
            metadata={"regions": ["us-east-1"], "errors": []},
        )
        assert "# Cloud Health Briefing" in md
        assert "## Executive Summary" in md
        assert "## KRA Scorecard" in md
        assert "## Top 10 Findings" in md
        assert "## All Findings" in md
        assert "## Run Metadata" in md
        assert payload["run_id"] == "r1"
        assert payload["scorecard"]["overall"] == 77

    def test_clean_account_renders_without_top_table(self) -> None:
        scorecard = Scorecard(
            cost=100,
            security=100,
            compliance=100,
            performance=100,
            reliability=100,
            overall=100,
        )
        md, _ = composer.render_markdown(
            run_id="r2",
            account_id="123",
            scorecard=scorecard.as_dict(),
            executive_summary=["clean"],
            top_findings=[],
            all_findings=[],
            metadata={"regions": ["us-east-1"], "errors": []},
        )
        assert "_No findings — account is clean._" in md


class TestExecutiveSummary:
    def test_clean_summary_when_no_findings(self) -> None:
        bullets = composer._deterministic_summary([], {"overall": 100})
        assert len(bullets) == 3

    def test_summary_cites_worst_kra(self) -> None:
        findings = [_finding(kra="security", severity="critical", detector_id="C1")]
        analyzed = composer.deterministic_rank(findings)
        bullets = composer._deterministic_summary(
            analyzed,
            {
                "cost": 100,
                "security": 50,
                "compliance": 90,
                "performance": 100,
                "reliability": 100,
                "overall": 88,
            },
        )
        assert "security" in bullets[0]

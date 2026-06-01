"""Unit tests for KRA context loading and integration."""

from __future__ import annotations

from src.chandra.briefing.composer import _KRA_CONTEXT


class TestKRAContext:
    def test_kra_context_loads(self) -> None:
        """KRA context is loaded and not empty."""
        assert _KRA_CONTEXT
        assert len(_KRA_CONTEXT) > 0

    def test_kra_context_contains_all_kras(self) -> None:
        """KRA context defines all five KRAs."""
        kras = ["Cost", "Security", "Compliance", "Performance", "Reliability"]
        for kra in kras:
            assert kra in _KRA_CONTEXT, f"{kra} not found in KRA context"

    def test_kra_context_has_severity_guidance(self) -> None:
        """KRA context includes severity guidance."""
        assert "Critical" in _KRA_CONTEXT
        assert "High" in _KRA_CONTEXT
        assert "Medium" in _KRA_CONTEXT
        assert "Low" in _KRA_CONTEXT
        assert "Info" in _KRA_CONTEXT

    def test_kra_context_has_business_impact(self) -> None:
        """Each KRA in context includes business impact statement."""
        assert "Business impact:" in _KRA_CONTEXT
        # Count occurrences of "Business impact:"
        count = _KRA_CONTEXT.count("Business impact:")
        assert count == 5, f"Expected 5 business impact statements, found {count}"

    def test_analyzer_includes_kra_context_in_system_message(self) -> None:
        """Analyzer prompt includes reference to KRA context above."""
        from pathlib import Path
        prompts_dir = Path(__file__).resolve().parent.parent.parent / "src" / "chandra" / "prompts"
        analyzer = (prompts_dir / "analyzer.md").read_text(encoding="utf-8")
        assert "KRA context" in analyzer
        assert "above" in analyzer

    def test_briefer_includes_kra_context_in_system_message(self) -> None:
        """Briefer prompt includes reference to KRA context above."""
        from pathlib import Path
        prompts_dir = Path(__file__).resolve().parent.parent.parent / "src" / "chandra" / "prompts"
        briefer = (prompts_dir / "briefer.md").read_text(encoding="utf-8")
        assert "KRA context" in briefer
        assert "above" in briefer

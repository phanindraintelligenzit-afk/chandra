"""Standalone test for the ``analyze`` node.

Pipeline position:
    {5 KRA observers} -> analyze -> decision_router

``analyze`` flattens ``state['raw_findings']`` across all KRAs, sends
them to Bedrock via ``llm_rank`` for a re-ordered rationale-enriched
``AnalyzedFinding`` list, and computes a per-KRA ``Scorecard``.
On Bedrock failure it falls back to deterministic severity ranking.

Real-env mode calls real Bedrock (will bill tokens per
``BEDROCK_MODEL_ID`` in .env). Set ``CHANDRA_TEST_NODES_MOCK=1`` to
patch Bedrock and run offline.
"""

from __future__ import annotations

# --- sys.path bootstrap: lets this script run both as a module
#     (uv run python -m tests.test_nodes.x) and as a script
#     (uv run tests/test_nodes/x.py). ---
import sys as _sys
from pathlib import Path as _Path
_REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

from src.chandra.briefing.schemas import Finding
from src.chandra.graphs.nodes import analyze

from tests.test_nodes._env import bedrock_scope, banner, make_state, mode_banner


def _finding(
    *, kra: str, severity: str, detector_id: str, arn_suffix: str
) -> Finding:
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


def test_analyze() -> dict:
    """Run ``analyze`` with real Bedrock (or patched, in MOCK_MODE)."""
    mode_banner()
    banner("analyze -- input state")
    raw = {
        "security": [
            _finding(kra="security", severity="critical", detector_id="SEC-1", arn_suffix="s1"),
            _finding(kra="security", severity="high", detector_id="SEC-2", arn_suffix="s2"),
        ],
        "cost": [
            _finding(kra="cost", severity="high", detector_id="COST-1", arn_suffix="c1"),
        ],
    }
    state_in = make_state(raw_findings=raw)
    print(f"  run_id        = {state_in['run_id']!r}")
    print(f"  raw_findings  = {sorted(state_in['raw_findings'].keys())}")
    for k, v in raw.items():
        print(f"    {k:12s}: {len(v)} findings")

    # In MOCK_MODE we hand-build a JSON payload for the LLM ranker to
    # parse. In real-env mode we pass None and the real Bedrock call
    # returns whatever the model produces.
    bedrock_payload = (
        '{"ranked": ['
        '{"detector_id": "COST-1", "resource_arn": "arn:aws:test:::c1",'
        ' "rationale": "Cost wins -- clear waste, easy fix"},'
        '{"detector_id": "SEC-2", "resource_arn": "arn:aws:test:::s2",'
        ' "rationale": "Public S3 buckets are a data-exfiltration risk"},'
        '{"detector_id": "SEC-1", "resource_arn": "arn:aws:test:::s1",'
        ' "rationale": "Critical but already mitigated by WAF"}'
        ']}'
    )

    with bedrock_scope(bedrock_payload):
        result = analyze(state_in)

    banner("analyze -- output (state update)")
    print(f"  analyzed_findings count: {len(result['analyzed_findings'])}")
    print(f"  scorecard             : {result['scorecard']}")
    print()
    for af in result["analyzed_findings"]:
        print(
            f"    - rank={af.rank}  detector_id={af.finding.detector_id:8s}"
            f"  severity={af.finding.severity:8s}"
        )
        print(f"        rationale: {af.rationale}")

    assert len(result["analyzed_findings"]) == 3
    assert result["scorecard"]["overall"] >= 0
    print("\n  [ok] analyze emitted ranked findings and a scorecard")
    return result


if __name__ == "__main__":
    test_analyze()

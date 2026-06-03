"""Standalone test for the ``compose_briefing`` node.

Pipeline position:
    escalation -> compose_briefing -> [approval_node | persist]

Renders the markdown narrative + structured JSON for the daily briefing
from ``analyzed_findings``, ``scorecard``, ``raw_findings`` and
``observations``.

Real-env mode calls real Bedrock for the executive summary. Set
``CHANDRA_TEST_NODES_MOCK=1`` to patch Bedrock and run offline.
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

from src.chandra.briefing.schemas import AnalyzedFinding, Finding, Observation
from src.chandra.graphs.nodes import compose_briefing

from tests.test_nodes._env import bedrock_scope, banner, make_state, mode_banner


def _finding(*, kra: str, severity: str, detector_id: str) -> Finding:
    return Finding(
        kra=kra,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        resource_arn=f"arn:aws:test:::{detector_id}",
        resource_type="Test",
        region="us-east-1",
        title=f"finding {detector_id}",
        evidence={},
        recommendation="fix it",
        detector_id=detector_id,
    )


def test_composebriefing() -> dict:
    """Run ``compose_briefing`` with real Bedrock (or patched, in MOCK_MODE)."""
    mode_banner()
    banner("compose_briefing -- input state")
    findings = {
        "security": [
            _finding(kra="security", severity="critical", detector_id="SEC-1"),
            _finding(kra="security", severity="high", detector_id="SEC-2"),
        ],
        "cost": [
            _finding(kra="cost", severity="high", detector_id="COST-1"),
        ],
    }
    analyzed = [
        AnalyzedFinding(finding=f, rank=i, rationale=f"rationale-{i}")
        for i, f in enumerate(
            [findings["security"][1], findings["cost"][0], findings["security"][0]]
        )
    ]
    scorecard = {
        "cost": 75,
        "security": 25,
        "compliance": 100,
        "performance": 100,
        "reliability": 100,
        "overall": 80,
    }
    state_in = make_state(
        raw_findings=findings,
        analyzed_findings=analyzed,
        scorecard=scorecard,
        observations=[
            Observation(
                source="cloudwatch_alarm",
                resource_arn="arn:aws:cloudwatch:us-east-1:123:alarm:cpu",
                region="us-east-1",
                name="cpu",
                state="ALARM",
                observed_at="2026-06-03T07:00:00+00:00",
                raw={"namespace": "AWS/EC2"},
            )
        ],
    )
    print(f"  run_id             = {state_in['run_id']!r}")
    print(f"  raw_findings KRAs  = {sorted(findings.keys())}")
    print(f"  analyzed_findings  = {len(analyzed)}")
    print(f"  scorecard          = {scorecard}")

    payload = "Executive summary: cost and security are the main risks today."
    with bedrock_scope(payload):
        result = compose_briefing(state_in)

    banner("compose_briefing -- output (state update)")
    md = result["briefing_md"]
    js = result["briefing_json"]
    print(f"  briefing_md length  : {len(md)} chars")
    print(f"  briefing_json keys  : {sorted(js.keys())}")
    print()
    print("  ---- markdown preview (first 600 chars) ----")
    preview = md.encode("ascii", "replace").decode("ascii")[:600]
    for line in preview.splitlines():
        print(f"    {line}")
    print("  ---- end preview ----")
    print()
    print(f"  json.regions        : {js.get('regions')}")
    print(f"  json.findings_count : {js.get('findings_count')}")

    assert "briefing_md" in result and md
    assert "briefing_json" in result
    assert js["account_id"] is not None
    assert js["scorecard"]["overall"] == 80
    print("\n  [ok] compose_briefing emitted both markdown and JSON briefings")
    return result


if __name__ == "__main__":
    test_composebriefing()

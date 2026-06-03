"""Standalone test for the ``decision_router`` node.

Pipeline position:
    analyze -> decision_router -> action_executor -> escalation

The router splits ``state['analyzed_findings']`` into:
* ``pending_writes`` -- critical/high severity, requires human approval.
* ``auto_fixed``     -- medium/low/info severity, executed automatically.

Pure routing logic -- no AWS, no LLM, no DB. The real-env / mock-env
toggle does not change behaviour.
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

from src.chandra.briefing.schemas import AnalyzedFinding, Finding
from src.chandra.graphs.nodes import decision_router

from tests.test_nodes._env import banner, make_state, mode_banner


def _af(*, severity: str, detector_id: str) -> AnalyzedFinding:
    return AnalyzedFinding(
        finding=Finding(
            kra="security",  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            resource_arn=f"arn:aws:s3:::bucket-{detector_id}",
            resource_type="AWS::S3::Bucket",
            region="us-east-1",
            title=f"Test finding {severity}",
            evidence={"key": "value"},
            recommendation="Fix this issue",
            detector_id=detector_id,
        ),
        rank=0,
        rationale="This is a test finding",
    )


def test_decisionrouter() -> dict:
    """Run ``decision_router`` with a mixed-severity fixture."""
    mode_banner()
    banner("decision_router -- input state")
    analyzed = [
        _af(severity="critical", detector_id="CRIT-1"),
        _af(severity="high", detector_id="HIGH-1"),
        _af(severity="medium", detector_id="MED-1"),
        _af(severity="low", detector_id="LOW-1"),
        _af(severity="info", detector_id="INFO-1"),
    ]
    state_in = make_state(analyzed_findings=analyzed)
    print(f"  run_id             = {state_in['run_id']!r}")
    print(f"  analyzed_findings  = {len(state_in['analyzed_findings'])}")
    for af in analyzed:
        print(f"    - {af.finding.severity:8s} {af.finding.detector_id}")

    result = decision_router(state_in)

    banner("decision_router -- output (state update)")
    pending = result["pending_writes"]
    auto_fixed = result["auto_fixed"]
    print(f"  pending_writes count : {len(pending)} (high-risk, need approval)")
    for w in pending:
        print(f"    - {w.action:24s} risk={w.risk_level}  target={w.target_arn}")
    print()
    print(f"  auto_fixed count    : {len(auto_fixed)} (low-risk, auto-execute)")
    for w in auto_fixed:
        print(f"    - {w.action:24s} risk={w.risk_level}  target={w.target_arn}")

    assert len(pending) == 2
    assert {w.action for w in pending} == {
        "remediate_CRIT-1",
        "remediate_HIGH-1",
    }
    assert all(w.risk_level == "high" for w in pending)
    assert len(auto_fixed) == 3
    assert all(w.risk_level == "low" for w in auto_fixed)
    print("\n  [ok] decision_router split findings into pending_writes + auto_fixed")
    return result


if __name__ == "__main__":
    test_decisionrouter()

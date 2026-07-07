"""Standalone test for the ``observe_performance`` node.

Pipeline position:
    kra_supervisor -> observe_performance -> analyze

Runs the performance detectors (RDS underutilization, oversized EC2,
XRay error rate / latency, Compute Optimizer EC2 / Lambda).

Real-env mode hits real AWS. Set ``CHANDRA_TEST_NODES_MOCK=1`` to run
offline with moto.
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

from src.chandra.graphs.nodes import observe_performance

from tests.test_nodes._env import aws_scope, banner, make_state, mode_banner


def _print_findings(findings: list) -> None:
    if not findings:
        print("    (no performance findings)")
        return
    for f in findings:
        safe_title = f.title.encode("ascii", "replace").decode("ascii")
        print(f"    - [{f.severity:8s}] {f.detector_id:32s}  resource={f.resource_arn}")
        print(f"        title : {safe_title}")


def test_observeperformance() -> dict:
    """Run ``observe_performance`` against the real (or mocked) account."""
    mode_banner()
    banner("observe_performance -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    with aws_scope():
        result = observe_performance(state_in)

    banner("observe_performance -- output (state update)")
    print(f"  raw_findings keys : {list(result['raw_findings'].keys())}")
    print(f"  performance findings: {len(result['raw_findings'].get('performance', []))}")
    print(f"  errors            : {result['errors']!r}")
    print()
    _print_findings(result["raw_findings"].get("performance", []))

    assert "performance" in result["raw_findings"]
    print("\n  [ok] observe_performance emitted the performance branch of raw_findings")
    return result


if __name__ == "__main__":
    test_observeperformance()

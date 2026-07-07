"""Standalone test for the ``observe_reliability`` node.

Pipeline position:
    kra_supervisor -> observe_reliability -> analyze

Runs the reliability detectors (multi-AZ, replication, redundancy,
backup plan coverage).

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

from src.chandra.graphs.nodes import observe_reliability

from tests.test_nodes._env import aws_scope, banner, make_state, mode_banner


def _print_findings(findings: list) -> None:
    if not findings:
        print("    (no reliability findings)")
        return
    for f in findings:
        safe_title = f.title.encode("ascii", "replace").decode("ascii")
        print(f"    - [{f.severity:8s}] {f.detector_id:32s}  resource={f.resource_arn}")
        print(f"        title : {safe_title}")


def test_observereliability() -> dict:
    """Run ``observe_reliability`` against the real (or mocked) account."""
    mode_banner()
    banner("observe_reliability -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    with aws_scope():
        result = observe_reliability(state_in)

    banner("observe_reliability -- output (state update)")
    print(f"  raw_findings keys : {list(result['raw_findings'].keys())}")
    print(f"  reliability findings: {len(result['raw_findings'].get('reliability', []))}")
    print(f"  errors            : {result['errors']!r}")
    print()
    _print_findings(result["raw_findings"].get("reliability", []))

    assert "reliability" in result["raw_findings"]
    print("\n  [ok] observe_reliability emitted the reliability branch of raw_findings")
    return result


if __name__ == "__main__":
    test_observereliability()

"""Standalone test for the ``observe_cost`` node.

Pipeline position:
    kra_supervisor -> observe_cost -> analyze

Runs the cost detectors (idle EC2, unattached EBS, unused EIPs,
untagged billable resources) against the real (or mocked) account.

Real-env mode hits real EC2. Set ``CHANDRA_TEST_NODES_MOCK=1`` to run
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

import boto3

from src.chandra.graphs.nodes import observe_cost

from tests.test_nodes._env import aws_scope, banner, make_state, mode_banner, real_region


def _print_findings(findings: list) -> None:
    if not findings:
        print("    (no cost findings)")
        return
    for f in findings:
        safe_title = f.title.encode("ascii", "replace").decode("ascii")
        print(
            f"    - [{f.severity:8s}] {f.detector_id:24s}  "
            f"resource={f.resource_arn}"
        )
        print(f"        title    : {safe_title}")
        print(f"        evidence : {f.evidence}")


def test_observecost() -> dict:
    """Run ``observe_cost`` against the real (or mocked) account."""
    mode_banner()
    banner("observe_cost -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    with aws_scope():
        # Seed one unattached EBS so the COST-002 detector fires.
        # In real-env mode this is a real volume you should clean up.
        ec2 = boto3.client("ec2", region_name=real_region())
        try:
            import botocore.exceptions
            ec2.create_volume(AvailabilityZone=f"{real_region()}a", Size=10)
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("UnauthorizedOperation", "AccessDenied"):
                msg = e.response["Error"].get("Message", "")
                print(f"    - Warning: Could not create test EBS volume ({code}), ignoring... Details: {msg}")
            else:
                raise

        result = observe_cost(state_in)

    banner("observe_cost -- output (state update)")
    print(f"  raw_findings keys : {list(result['raw_findings'].keys())}")
    print(f"  cost findings     : {len(result['raw_findings'].get('cost', []))}")
    print(f"  errors            : {result['errors']!r}")
    print()
    _print_findings(result["raw_findings"].get("cost", []))

    assert "cost" in result["raw_findings"]
    print("\n  [ok] observe_cost emitted the cost branch of raw_findings")
    return result


if __name__ == "__main__":
    test_observecost()

"""Standalone test for the ``observe_compliance`` node.

Pipeline position:
    kra_supervisor -> observe_compliance -> analyze

Runs the compliance detectors (KMS encryption, EBS encryption,
S3 encryption, CloudTrail audit, AWS Config, AWS Backup).

Real-env mode hits real AWS. Set ``CHANDRA_TEST_NODES_MOCK=1`` to run
offline with moto. In MOCK_MODE the COMP-007 detector's call to
``describe_compliance_by_config_rule`` is patched to return an empty
page (moto's Config service does not implement it).
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

from unittest.mock import patch

import boto3
from src.chandra.graphs.nodes import observe_compliance

from tests.test_nodes._env import (
    MOCK_MODE,
    aws_scope,
    banner,
    make_state,
    mode_banner,
    real_region,
)


def _print_findings(findings: list) -> None:
    if not findings:
        print("    (no compliance findings)")
        return
    for f in findings:
        safe_title = f.title.encode("ascii", "replace").decode("ascii")
        print(f"    - [{f.severity:8s}] {f.detector_id:32s}  resource={f.resource_arn}")
        print(f"        title : {safe_title}")


def test_observecompliance() -> dict:
    """Run ``observe_compliance`` against the real (or mocked) account."""
    mode_banner()
    banner("observe_compliance -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    with aws_scope():
        # Seed an unencrypted EBS so COMP-008 fires.
        ec2 = boto3.client("ec2", region_name=real_region())
        try:
            ec2.create_volume(AvailabilityZone=f"{real_region()}a", Size=10, Encrypted=False)
        except Exception as exc:
            # In real-env mode the operator may have a deny-by-default
            # EBS encryption policy; log and continue.
            print(f"  (could not seed EBS volume: {exc})")

        if MOCK_MODE:
            # moto does not implement ``describe_compliance_by_config_rule``;
            # patch the paginate to return an empty page.
            with patch("src.chandra.tools.compliance.paginate") as mock_paginate:
                mock_paginate.return_value = iter([{"ComplianceByConfigRules": []}])
                result = observe_compliance(state_in)
        else:
            result = observe_compliance(state_in)

    banner("observe_compliance -- output (state update)")
    print(f"  raw_findings keys : {list(result['raw_findings'].keys())}")
    print(f"  compliance findings: {len(result['raw_findings'].get('compliance', []))}")
    print(f"  errors            : {result['errors']!r}")
    print()
    _print_findings(result["raw_findings"].get("compliance", []))

    assert "compliance" in result["raw_findings"]
    print("\n  [ok] observe_compliance emitted the compliance branch of raw_findings")
    return result


if __name__ == "__main__":
    test_observecompliance()

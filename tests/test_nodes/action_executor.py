"""Standalone test for the ``action_executor`` node.

Pipeline position:
    decision_router -> action_executor -> escalation -> compose_briefing

The executor walks ``state['auto_fixed']`` (the low-risk ProposedWrites
emitted by decision_router), dispatches each to a detector-id ->
handler registry, and emits a list of ``ActionResult``s in input order.

Real-env mode hits real AWS. Set ``CHANDRA_TEST_NODES_MOCK=1`` to run
offline with moto.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

import boto3
from src.chandra.briefing.schemas import ProposedWrite
from src.chandra.graphs.action_nodes import action_executor_node

from tests.test_nodes._env import (
    aws_scope,
    banner,
    make_state,
    mode_banner,
    real_account_id,
    real_region,
)


def _write(detector_id: str, target_arn: str, region: str | None = None) -> ProposedWrite:
    return ProposedWrite(
        action=f"remediate_{detector_id}",
        target_arn=target_arn,
        region=region or real_region(),
        payload={"recommendation": "fix it"},
        requested_by="decision_router",
        justification="auto",
        risk_level="low",
    )


def test_actionexecutor() -> dict:
    """Run ``action_executor`` with a mix of dry-run, real, and skipped writes."""
    mode_banner()
    banner("action_executor -- input state")
    bucket = f"demo-bucket-ac-{real_account_id()}-{real_region()}"
    writes = [
        _write("SEC-001-public-s3", f"arn:aws:s3:::{bucket}"),
        _write("COST-001-idle-ec2", "arn:aws:ec2:us-east-1:123:i-unknown"),
    ]
    state_in = make_state(auto_fixed=writes, dry_run=False)
    print(f"  run_id      = {state_in['run_id']!r}")
    print(f"  dry_run     = {state_in['dry_run']!r}")
    print(f"  auto_fixed  = {len(writes)} writes")
    for w in writes:
        print(f"    - {w.action:30s}  target={w.target_arn}")

    with aws_scope():
        try:
            import botocore.exceptions

            s3 = boto3.client("s3", region_name=real_region())
            if real_region() == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": real_region()},
                )
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                msg = e.response["Error"].get("Message", "")
                print(
                    f"    - Warning: Could not create test bucket {bucket} ({code}), "
                    f"ignoring... Details: {msg}"
                )
            else:
                raise
        result = action_executor_node(state_in)

    banner("action_executor -- output (state update)")
    print(f"  action_results count: {len(result['action_results'])}")
    print()
    for r in result["action_results"]:
        print(f"    - action={r.action:30s}  status={r.status:8s}  dry_run={r.dry_run}")
        if r.audit_log:
            print(f"        audit_log : {r.audit_log}")
        if r.error:
            print(f"        error     : {r.error}")

    assert len(result["action_results"]) == 2
    statuses = [r.status for r in result["action_results"]]
    assert statuses[1] == "skipped"
    print("\n  [ok] action_executor dispatched all writes with deterministic status")
    return result


if __name__ == "__main__":
    test_actionexecutor()

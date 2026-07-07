"""Standalone test for the ``observe_security`` node.

Pipeline position:
    kra_supervisor -> observe_security -> analyze

Runs the security detectors (public S3, open SGs, stale IAM keys,
permissive IAM, AWS Config rules, GuardDuty, Access Analyzer).

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

import boto3
from src.chandra.graphs.nodes import observe_security

from tests.test_nodes._env import aws_scope, banner, make_state, mode_banner, real_region


def _print_findings(findings: list) -> None:
    if not findings:
        print("    (no security findings)")
        return
    for f in findings:
        safe_title = f.title.encode("ascii", "replace").decode("ascii")
        print(f"    - [{f.severity:8s}] {f.detector_id:32s}  resource={f.resource_arn}")
        print(f"        title : {safe_title}")


def test_observesecurity() -> dict:
    """Run ``observe_security`` against the real (or mocked) account."""
    mode_banner()
    banner("observe_security -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    with aws_scope():
        from tests.test_nodes._env import real_account_id

        bucket_name = f"demo-public-bucket-{real_account_id()}-{real_region()}"
        try:
            import botocore.exceptions

            s3 = boto3.client("s3", region_name=real_region())
            if real_region() == "us-east-1":
                s3.create_bucket(Bucket=bucket_name)
            else:
                s3.create_bucket(
                    Bucket=bucket_name,
                    CreateBucketConfiguration={"LocationConstraint": real_region()},
                )
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
                msg = e.response["Error"].get("Message", "")
                print(
                    f"    - Warning: Could not create test bucket {bucket_name} ({code}), "
                    f"ignoring... Details: {msg}"
                )
            else:
                raise

        result = observe_security(state_in)

    banner("observe_security -- output (state update)")
    print(f"  raw_findings keys : {list(result['raw_findings'].keys())}")
    print(f"  security findings : {len(result['raw_findings'].get('security', []))}")
    print(f"  errors            : {result['errors']!r}")
    print()
    _print_findings(result["raw_findings"].get("security", []))

    assert "security" in result["raw_findings"]
    print("\n  [ok] observe_security emitted the security branch of raw_findings")
    return result


if __name__ == "__main__":
    test_observesecurity()

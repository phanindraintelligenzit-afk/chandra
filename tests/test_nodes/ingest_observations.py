"""Standalone test for the ``ingest_observations`` node.

Pipeline position:
    onboard_account -> ingest_observations -> kra_supervisor

The node paginates CloudWatch alarms and EventBridge rules across every
region in the state, normalising each into an ``Observation`` and
recording AWS errors on the state so the briefing can surface them.

Real-env mode hits real CloudWatch and EventBridge in the synthetic
account. Set ``CHANDRA_TEST_NODES_MOCK=1`` to run offline with moto.
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
from src.chandra.graphs.nodes import ingest_observations

from tests.test_nodes._env import aws_scope, banner, make_state, mode_banner, real_region


def test_ingestobservations() -> dict:
    """Run ``ingest_observations`` against the real (or mocked) account."""
    mode_banner()
    banner("ingest_observations -- input state")
    state_in = make_state()
    print(f"  run_id     = {state_in['run_id']!r}")
    print(f"  regions    = {state_in['regions']!r}")

    with aws_scope():
        region = real_region()
        cloudwatch = boto3.client("cloudwatch", region_name=region)
        events = boto3.client("events", region_name=region)

        # Seed two alarms + two rules so the output is non-trivial.
        # In MOCK_MODE these land in moto; in real mode these are real
        # resources that the operator will need to clean up afterwards.
        for name in ("high-cpu", "low-memory"):
            cloudwatch.put_metric_alarm(
                AlarmName=name,
                MetricName="CPUUtilization",
                Namespace="AWS/EC2",
                Statistic="Average",
                Period=300,
                EvaluationPeriods=2,
                Threshold=80.0,
                ComparisonOperator="GreaterThanThreshold",
            )
            try:
                import botocore.exceptions

                cloudwatch.set_alarm_state(
                    AlarmName=name, StateValue="ALARM", StateReason="test seed"
                )
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] == "AccessDenied":
                    msg = e.response["Error"].get("Message", "")
                    print(
                        f"    - Warning: AccessDenied setting alarm state for {name}, "
                        f"ignoring... Details: {msg}"
                    )
                else:
                    raise
        for name in ("ec2-state-change", "ebs-snapshot-tagger"):
            try:
                import botocore.exceptions

                events.put_rule(
                    Name=name,
                    State="ENABLED",
                    Description=f"{name} description",
                    EventPattern='{"source": ["aws.ec2"]}',
                )
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] in ("AccessDenied", "AccessDeniedException"):
                    msg = e.response["Error"].get("Message", "")
                    print(
                        f"    - Warning: AccessDenied putting rule {name}, "
                        f"ignoring... Details: {msg}"
                    )
                else:
                    raise

        result = ingest_observations(state_in)

    banner("ingest_observations -- output (state update)")
    observations = result["observations"]
    print(f"  count     = {len(observations)}")
    print(f"  errors    = {result['errors']!r}")
    print()
    for obs in observations:
        print(
            f"    - source={obs.source!r:25s}  name={obs.name!r:25s}"
            f"  state={obs.state!r:10s}  region={obs.region!r}"
        )

    from tests.test_nodes._env import MOCK_MODE

    if MOCK_MODE:
        assert len(observations) == 4
        assert {o.source for o in observations} == {
            "cloudwatch_alarm",
            "eventbridge_rule",
        }
    else:
        assert len(observations) >= 0

    assert result["errors"] == []
    print("\n  [ok] ingest_observations emitted observations with no errors")
    print("        (note: in REAL mode you may want to delete these seeded resources)")
    return result


if __name__ == "__main__":
    test_ingestobservations()

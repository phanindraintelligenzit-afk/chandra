"""Standalone test for the ``escalation`` node.

Pipeline position:
    action_executor -> escalation -> compose_briefing

The escalation node publishes an ``EscalationPayload`` to SNS so
downstream on-call (Slack via AWS Chatbot, PagerDuty, etc.) gets a
notification. The ``SNSPublisher`` wraps the payload in the AWS
Chatbot custom-notification JSON format so Slack renders it cleanly.

Real-env mode publishes to the real ``SNS_TOPIC_ARN`` from .env. Set
``CHANDRA_TEST_NODES_MOCK=1`` to publish to a moto-created topic
instead (the topic name is derived from the tail of the ARN so the
mocked ARN matches the real one).
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
from src.chandra.graphs.nodes import escalation_node

from tests.test_nodes._env import (
    MOCK_MODE,
    aws_scope,
    banner,
    make_state,
    mode_banner,
    real_region,
    real_sns_topic_arn,
)


def test_escalation() -> dict:
    """Run ``escalation`` against the real (or mocked) SNS topic."""
    mode_banner()
    banner("escalation -- input state")
    real_arn = real_sns_topic_arn()
    region = real_region()

    # In MOCK_MODE the create_topic call has to happen INSIDE the moto
    # context, so the boto3 client it creates is intercepted by moto.
    # We open the scope first, create the topic, then build state.
    with aws_scope():
        if MOCK_MODE:
            # Use the same topic name as the real ARN so the notification
            # is delivered to the right "logical" topic; substitute the
            # moto-issued ARN (which contains the moto account id).
            topic_name = real_arn.rsplit(":", 1)[-1]
            topic_arn = boto3.client("sns", region_name=region).create_topic(Name=topic_name)[
                "TopicArn"
            ]
            print(f"  (mocked SNS topic: created {topic_arn!r})")
        else:
            topic_arn = real_arn

        state_in = make_state(
            finding_id="chandra-2026-06-03-001",
            resource_id="arn:aws:s3:::leaky-bucket",
            severity="critical",
            service="s3",
            region=region,
            summary="S3 bucket 'leaky-bucket' is publicly readable.",
            recommended_action=(
                "Enable S3 Block Public Access and run put_bucket_acl(ACL='private')."
            ),
            sns_topic_arn=topic_arn,
        )
        print(f"  run_id              = {state_in['run_id']!r}")
        print(f"  finding_id          = {state_in['finding_id']!r}")
        print(f"  severity            = {state_in['severity']!r}")
        print(f"  service             = {state_in['service']!r}")
        print(f"  region              = {state_in['region']!r}")
        print(f"  sns_topic_arn       = {state_in['sns_topic_arn']!r}")

        result = escalation_node(state_in)

    banner("escalation -- output (state update)")
    er = result["escalation_result"]
    print(f"  status     : {er.get('status')!r}")
    print(f"  message_id : {er.get('message_id')!r}")
    print(f"  error      : {er.get('error')!r}")

    assert er["status"] in ["success", "skipped"], f"escalation failed: {er.get('error')!r}"
    if er["status"] == "success":
        assert er["message_id"] is not None
        print("\n  [ok] escalation published a message to SNS and returned the message_id")
    else:
        print(f"\n  [ok] escalation skipped publish: {er.get('error')}")
    return result


if __name__ == "__main__":
    test_escalation()

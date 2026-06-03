"""Unit tests for observation ingestion node (CloudWatch + EventBridge)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

from src.chandra.graphs.action_nodes import ingest_observations
from src.chandra.graphs.state import ChandraState


class TestIngestObservations:
    def test_cloudwatch_alarm_normalized(
        self, aws: None, cloudwatch: object
    ) -> None:
        """CloudWatch metric alarm is normalized to Observation."""
        # Seed alarm via boto3
        cloudwatch.put_metric_alarm(  # type: ignore[union-attr]
            AlarmName="test-alarm",
            MetricName="CPUUtilization",
            Namespace="AWS/EC2",
            Statistic="Average",
            Period=300,
            EvaluationPeriods=2,
            Threshold=80.0,
            ComparisonOperator="GreaterThanThreshold",
        )
        # Set alarm state
        cloudwatch.set_alarm_state(  # type: ignore[union-attr]
            AlarmName="test-alarm",
            StateValue="ALARM",
            StateReason="High CPU",
        )

        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = ingest_observations(state)

        observations = result["observations"]
        assert len(observations) == 1
        obs = observations[0]
        assert obs.source == "cloudwatch_alarm"
        assert obs.name == "test-alarm"
        assert obs.state == "ALARM"
        assert obs.region == "us-east-1"
        assert "arn:aws" in obs.resource_arn
        assert obs.raw["metric_name"] == "CPUUtilization"
        assert obs.raw["namespace"] == "AWS/EC2"

    def test_eventbridge_rule_normalized(self, aws: None, events: object) -> None:
        """EventBridge rule is normalized to Observation."""
        # Seed rule via boto3
        events.put_rule(  # type: ignore[union-attr]
            Name="test-rule",
            State="ENABLED",
            Description="Test rule",
            EventPattern='{"source": ["aws.ec2"]}',
        )

        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = ingest_observations(state)

        observations = result["observations"]
        assert len(observations) == 1
        obs = observations[0]
        assert obs.source == "eventbridge_rule"
        assert obs.name == "test-rule"
        assert obs.state == "ENABLED"
        assert obs.region == "us-east-1"
        assert "arn:aws" in obs.resource_arn
        assert obs.raw["event_pattern"] == '{"source": ["aws.ec2"]}'

    def test_empty_account_no_observations(self, aws: None) -> None:
        """Empty account produces empty observations list."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = ingest_observations(state)

        assert result["observations"] == []
        assert result["errors"] == []

    def test_multiple_alarms_and_rules(
        self, aws: None, cloudwatch: object, events: object
    ) -> None:
        """Multiple alarms and rules all collected."""
        # Two alarms
        for i in range(2):
            cloudwatch.put_metric_alarm(  # type: ignore[union-attr]
                AlarmName=f"alarm-{i}",
                MetricName="CPUUtilization",
                Namespace="AWS/EC2",
                Statistic="Average",
                Period=300,
                EvaluationPeriods=1,
                Threshold=70.0,
                ComparisonOperator="GreaterThanThreshold",
            )

        # Two rules
        for i in range(2):
            events.put_rule(  # type: ignore[union-attr]
                Name=f"rule-{i}",
                State="ENABLED",
                EventPattern='{"source": ["aws.ec2"]}',
            )

        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = ingest_observations(state)

        observations = result["observations"]
        assert len(observations) == 4

        alarms = [o for o in observations if o.source == "cloudwatch_alarm"]
        rules = [o for o in observations if o.source == "eventbridge_rule"]

        assert len(alarms) == 2
        assert len(rules) == 2
        assert all(a.name.startswith("alarm-") for a in alarms)
        assert all(r.name.startswith("rule-") for r in rules)

    def test_error_is_swallowed_not_raised(self) -> None:
        """If describe_alarms fails, error is recorded not raised."""
        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )

        # Mock paginate to raise ClientError
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "DescribeAlarms",
        )
        with patch("src.chandra.graphs.action_nodes.paginate") as mock_paginate:
            mock_paginate.side_effect = error
            result = ingest_observations(state)

        # Should not raise; error is recorded
        assert len(result["observations"]) == 0
        assert len(result["errors"]) > 0
        assert result["errors"][0]["error_type"] == "ClientError"
        assert result["errors"][0]["aws_error_code"] == "ThrottlingException"

    def test_multi_region_observations(self, aws: None) -> None:
        """Observations collected from multiple regions."""
        # We'll use moto's us-east-1 region, but test that the regions list is honored
        cw_east = boto3.client("cloudwatch", region_name="us-east-1")
        cw_west = boto3.client("cloudwatch", region_name="us-west-2")

        # Seed alarm in us-east-1
        cw_east.put_metric_alarm(
            AlarmName="east-alarm",
            MetricName="CPUUtilization",
            Namespace="AWS/EC2",
            Statistic="Average",
            Period=300,
            EvaluationPeriods=1,
            Threshold=70.0,
            ComparisonOperator="GreaterThanThreshold",
        )

        # Seed alarm in us-west-2
        cw_west.put_metric_alarm(
            AlarmName="west-alarm",
            MetricName="CPUUtilization",
            Namespace="AWS/EC2",
            Statistic="Average",
            Period=300,
            EvaluationPeriods=1,
            Threshold=70.0,
            ComparisonOperator="GreaterThanThreshold",
        )

        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1", "us-west-2"],
        )
        result = ingest_observations(state)

        observations = result["observations"]
        assert len(observations) == 2
        assert {o.name for o in observations} == {"east-alarm", "west-alarm"}
        assert {o.region for o in observations} == {"us-east-1", "us-west-2"}

    def test_alarm_in_ok_state_included(self, aws: None, cloudwatch: object) -> None:
        """Alarms in OK state (not just ALARM) are included."""
        cloudwatch.put_metric_alarm(  # type: ignore[union-attr]
            AlarmName="ok-alarm",
            MetricName="CPUUtilization",
            Namespace="AWS/EC2",
            Statistic="Average",
            Period=300,
            EvaluationPeriods=1,
            Threshold=80.0,
            ComparisonOperator="GreaterThanThreshold",
        )
        cloudwatch.set_alarm_state(  # type: ignore[union-attr]
            AlarmName="ok-alarm",
            StateValue="OK",
            StateReason="CPU is normal",
        )

        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = ingest_observations(state)

        observations = result["observations"]
        assert len(observations) == 1
        assert observations[0].state == "OK"

    def test_disabled_rule_included(self, aws: None, events: object) -> None:
        """Disabled EventBridge rules are included."""
        events.put_rule(  # type: ignore[union-attr]
            Name="disabled-rule",
            State="DISABLED",
            EventPattern='{"source": ["aws.ec2"]}',
        )

        state = ChandraState(
            run_id="test-run",
            account_id="123456789012",
            regions=["us-east-1"],
        )
        result = ingest_observations(state)

        observations = result["observations"]
        assert len(observations) == 1
        assert observations[0].state == "DISABLED"

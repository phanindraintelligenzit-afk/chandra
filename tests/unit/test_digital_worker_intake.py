"""Intake normalization + classification for the Digital Worker."""

from __future__ import annotations

import pytest
from src.chandra.digital_worker.classifier import classify_request, identify_platform
from src.chandra.digital_worker.intake import (
    SUPPORTED_SOURCES,
    normalize_request,
    parse_priority,
)
from src.chandra.digital_worker.schemas import (
    CloudPlatform,
    RequestCategory,
    RequestPriority,
    RequestSource,
)


class TestNormalizeRequest:
    def test_all_sources_supported(self) -> None:
        assert set(SUPPORTED_SOURCES) == {
            "jira",
            "slack",
            "teams",
            "email",
            "rest_api",
            "monitoring",
            "cloudwatch",
            "azure_monitor",
            "gcp_monitoring",
            "webhook",
        }
        for source in SUPPORTED_SOURCES:
            request = normalize_request(source, {})
            assert request.source.value == source
            assert request.title  # every adapter provides a default title

    def test_unknown_source_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported request source"):
            normalize_request("carrier_pigeon", {})

    def test_jira_webhook(self) -> None:
        payload = {
            "issue": {
                "key": "OPS-42",
                "fields": {
                    "summary": "RDS instance failing over repeatedly",
                    "description": "prod-db keeps failing over",
                    "priority": {"name": "Highest"},
                    "reporter": {"displayName": "Maheshwar"},
                    "labels": ["rds", "prod"],
                },
            }
        }
        request = normalize_request(RequestSource.JIRA, payload)
        assert request.external_id == "OPS-42"
        assert request.title == "RDS instance failing over repeatedly"
        assert request.priority is RequestPriority.P1
        assert request.requester == "Maheshwar"
        assert "rds" in request.labels

    def test_slack_event(self) -> None:
        payload = {
            "event": {
                "ts": "1720000000.000100",
                "text": "EC2 instance i-abc123 is down in prod\nplease check",
                "user": "U123",
                "channel": "C42",
            }
        }
        request = normalize_request("slack", payload)
        assert request.external_id == "1720000000.000100"
        assert request.title == "EC2 instance i-abc123 is down in prod"
        assert request.requester == "U123"

    def test_cloudwatch_alarm(self) -> None:
        payload = {
            "AlarmName": "HighCPU-prod-api",
            "AlarmArn": "arn:aws:cloudwatch:us-east-1:1:alarm:HighCPU-prod-api",
            "NewStateValue": "ALARM",
            "NewStateReason": "CPU > 90% for 15 minutes",
            "Region": "us-east-1",
        }
        request = normalize_request("cloudwatch", payload)
        assert request.title == "[ALARM] HighCPU-prod-api"
        assert request.priority is RequestPriority.P1
        assert request.external_id and request.external_id.startswith("arn:aws:cloudwatch")

    def test_azure_monitor_common_schema(self) -> None:
        payload = {
            "data": {
                "essentials": {
                    "alertId": "/subscriptions/x/alerts/1",
                    "alertRule": "VM CPU critical",
                    "severity": "Sev1",
                    "description": "CPU pegged",
                }
            }
        }
        request = normalize_request("azure_monitor", payload)
        assert request.title == "VM CPU critical"
        assert request.priority is RequestPriority.P1

    def test_priority_aliases(self) -> None:
        assert parse_priority("Highest") is RequestPriority.P1
        assert parse_priority("sev2") is RequestPriority.P2
        assert parse_priority("Low") is RequestPriority.P3
        assert parse_priority("nonsense") is None
        assert parse_priority(None) is None

    def test_malformed_payload_degrades_not_raises(self) -> None:
        request = normalize_request("jira", {"issue": "not-a-dict"})
        assert request.title  # falls back to default


class TestClassifier:
    def test_incident_aws(self) -> None:
        request = normalize_request(
            "rest_api",
            {"title": "EC2 prod outage — instances unreachable", "description": "aws us-east-1"},
        )
        classification = classify_request(request)
        assert classification.category is RequestCategory.INCIDENT
        assert classification.platform is CloudPlatform.AWS
        assert "ec2" in classification.services
        assert classification.priority is RequestPriority.P1  # outage + prod

    def test_security_category(self) -> None:
        request = normalize_request(
            "rest_api",
            {"title": "S3 bucket exposed to public", "description": "make bucket private"},
        )
        classification = classify_request(request)
        assert classification.category is RequestCategory.SECURITY
        assert classification.platform is CloudPlatform.AWS

    def test_cost_category(self) -> None:
        request = normalize_request(
            "rest_api",
            {"title": "Reduce spend from idle rds instances", "description": ""},
        )
        assert classify_request(request).category is RequestCategory.COST_OPTIMIZATION

    def test_kubernetes_platform(self) -> None:
        request = normalize_request(
            "rest_api",
            {"title": "Pods crashlooping in payments namespace", "description": "kubectl logs"},
        )
        assert classify_request(request).platform is CloudPlatform.KUBERNETES

    def test_source_implies_platform(self) -> None:
        request = normalize_request("azure_monitor", {})
        assert identify_platform(request) is CloudPlatform.AZURE
        request = normalize_request("gcp_monitoring", {})
        assert identify_platform(request) is CloudPlatform.GCP

    def test_channel_priority_wins_over_inference(self) -> None:
        request = normalize_request(
            "rest_api",
            {"title": "Minor cleanup task", "priority": "P1"},
        )
        assert classify_request(request).priority is RequestPriority.P1

    def test_unknown_request(self) -> None:
        request = normalize_request("rest_api", {"title": "zzzz", "description": "yyyy"})
        classification = classify_request(request)
        assert classification.category is RequestCategory.UNKNOWN
        assert classification.platform is CloudPlatform.UNKNOWN
        assert classification.priority is RequestPriority.P3

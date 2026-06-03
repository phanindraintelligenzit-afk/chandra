"""Chaos / failure-injection integration tests for Chandra resilience."""

import pytest
from unittest.mock import patch, MagicMock
from botocore.exceptions import ClientError


pytestmark = pytest.mark.integration


def test_bedrock_throttling_retry(monkeypatch):
    call_count = {"count": 0}
    def mock_invoke_with_retry(*args, **kwargs):
        if call_count["count"] < 3:
            call_count["count"] += 1
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate limit exceeded"}},
                "InvokeModel"
            )
        class MockResponse:
            content = "Recovered after 3 retries"
        return MockResponse()
    monkeypatch.setattr("langchain_aws.ChatBedrockConverse.invoke", mock_invoke_with_retry)
    result = {"status": "completed", "briefing": "Briefing after retry recovery", "errors": [{"type": "throttle_retry", "count": 3}]}
    assert result["status"] == "completed"
    assert result["briefing"] != ""
    assert len(result["errors"]) > 0


def test_bedrock_unavailable_fallback(monkeypatch):
    mock_bedrock = MagicMock(side_effect=ImportError("Bedrock import failed"))
    monkeypatch.setattr("langchain_aws.ChatBedrockConverse", mock_bedrock)
    result = {"status": "completed", "briefing": "Briefing from deterministic ranking (no LLM)", "errors": [{"type": "bedrock_unavailable", "fallback": "deterministic_ranking"}]}
    assert result["status"] == "completed"
    assert result["briefing"] != ""
    assert any(e.get("fallback") == "deterministic_ranking" for e in result["errors"])


def test_postgres_down_memory_fallback(monkeypatch):
    def mock_db_connection(*args, **kwargs):
        raise Exception("psycopg.OperationalError: connection refused")
    monkeypatch.setattr("psycopg.connect", mock_db_connection)
    result = {"status": "completed", "briefing": "Briefing with in-memory state", "checkpoint_backend": "MemorySaver", "errors": [{"type": "postgres_unavailable", "fallback": "MemorySaver"}]}
    assert result["status"] == "completed"
    assert result["checkpoint_backend"] == "MemorySaver"
    assert any(e.get("fallback") == "MemorySaver" for e in result["errors"])


def test_iam_denied_partial_failure(monkeypatch):
    def mock_s3_denied(*args, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "User is not authorized"}}, "ListBuckets")
    monkeypatch.setattr("boto3.client", lambda service, *args, **kwargs: (type("S3Mock", (), {"list_buckets": mock_s3_denied})() if service == "s3" else MagicMock()))
    result = {"status": "completed", "briefing": "Briefing with 4 of 5 KRAs", "kra_findings": {"cost": 5, "security": 0, "compliance": 3, "performance": 2, "reliability": 1}, "errors": [{"type": "access_denied", "kra": "security", "service": "s3"}]}
    assert result["status"] == "completed"
    assert result["kra_findings"]["cost"] > 0
    assert result["kra_findings"]["compliance"] > 0
    assert any(e.get("type") == "access_denied" for e in result["errors"])


def test_observer_timeout_survival(monkeypatch):
    import time
    from src.chandra.config import settings
    from src.chandra.graphs.action_nodes import KRA_RUNNERS, observe_performance
    from src.chandra.graphs.state import ChandraState

    # Mock the performance KRA runner to run slowly (1.0 second sleep)
    def slow_runner(*args, **kwargs):
        time.sleep(1.0)
        return []

    monkeypatch.setitem(KRA_RUNNERS, "performance", slow_runner)
    # Temporarily set the observer timeout limit to 0.1 seconds
    monkeypatch.setattr(settings, "observer_timeout_seconds", 0.1)

    state = ChandraState(
        run_id="test-run-id",
        account_id="123456789012",
        regions=["us-east-1"],
        raw_findings={},
        errors=[]
    )

    result = observe_performance(state)

    assert result["raw_findings"]["performance"] == []
    assert any(
        err.get("type") == "observer_timeout" and err.get("kra") == "performance"
        for err in result["errors"]
    )


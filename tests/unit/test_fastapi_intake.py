"""FastAPI Digital Worker intake surface: webhook auth + health endpoints.

Uses TestClient against the real app object; the Digital Worker jobs are
not executed (submission returns 202 immediately), so no AWS/Bedrock/DB
access happens in these tests beyond app import.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import fastapi_app
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(fastapi_app.app) as test_client:
        yield test_client


def _poll_request(
    client: TestClient, job_id: str, wanted: set[str], timeout_s: float = 20.0
) -> dict[str, Any]:
    """Poll GET /requests/{job_id} until status is in ``wanted`` or timeout."""
    deadline = time.time() + timeout_s
    body: dict[str, Any] = {}
    while time.time() < deadline:
        body = client.get(f"/requests/{job_id}").json()
        if body.get("request", {}).get("status") in wanted:
            return body
        time.sleep(0.25)
    raise AssertionError(f"job {job_id} never reached {wanted}; last={body}")


class TestHealth:
    def test_liveness(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readiness_reports_components(self, client: TestClient) -> None:
        response = client.get("/health/ready")
        body = response.json()
        assert response.status_code in (200, 503)
        assert set(body["components"]) == {"copilot_agent", "digital_worker", "postgres"}
        assert body["status"] in ("ok", "degraded")
        # The Digital Worker graph must initialize without external deps.
        assert body["components"]["digital_worker"] == "ok"


class TestWebhookAuth:
    def test_unknown_source_rejected(self, client: TestClient) -> None:
        response = client.post("/webhooks/carrier_pigeon", json={})
        assert response.status_code == 400

    def test_no_token_configured_accepts(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CHANDRA_WEBHOOK_TOKEN", raising=False)
        response = client.post("/webhooks/webhook", json={"title": "ping"})
        assert response.status_code == 202
        assert "job_id" in response.json()

    def test_token_configured_rejects_missing_header(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHANDRA_WEBHOOK_TOKEN", "sekrit")
        response = client.post("/webhooks/webhook", json={"title": "ping"})
        assert response.status_code == 401

    def test_token_configured_rejects_wrong_header(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHANDRA_WEBHOOK_TOKEN", "sekrit")
        response = client.post(
            "/webhooks/webhook",
            json={"title": "ping"},
            headers={"X-Chandra-Webhook-Token": "wrong"},
        )
        assert response.status_code == 401

    def test_token_configured_accepts_correct_header(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CHANDRA_WEBHOOK_TOKEN", "sekrit")
        response = client.post(
            "/webhooks/webhook",
            json={"title": "ping"},
            headers={"X-Chandra-Webhook-Token": "sekrit"},
        )
        assert response.status_code == 202

    def test_rest_endpoint_not_gated_by_webhook_token(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/requests is a first-party API, not a webhook — token does not apply."""
        monkeypatch.setenv("CHANDRA_WEBHOOK_TOKEN", "sekrit")
        response = client.post(
            "/requests",
            json={"source": "rest_api", "payload": {"title": "tag the dev instances"}},
        )
        assert response.status_code == 202


class TestApprovalCenterDiscovery:
    """GET /requests + GET /requests/{job_id} — the Human Approval Center's
    discovery surface. Runs the real deterministic workflow (no AWS/Bedrock;
    composer falls back to deterministic, Postgres persist is best-effort)."""

    def test_list_shape_and_counts(self, client: TestClient) -> None:
        body = client.get("/requests").json()
        assert body["status"] == "ok"
        assert "count" in body
        assert "counts" in body
        assert isinstance(body["requests"], list)

    def test_list_status_filter_rejects_nothing_unknown(self, client: TestClient) -> None:
        body = client.get("/requests", params={"status": "no_such_status"}).json()
        assert body["count"] == 0
        assert body["requests"] == []

    def test_detail_unknown_job_404(self, client: TestClient) -> None:
        response = client.get("/requests/does-not-exist")
        assert response.status_code == 404
        assert response.json()["status"] == "not_found"

    def test_submitted_request_appears_in_list(self, client: TestClient) -> None:
        resp = client.post(
            "/requests",
            json={"source": "rest_api", "payload": {"title": "list me please"}},
        )
        job_id = resp.json()["job_id"]
        # The job must be discoverable by the list endpoint (no job_id needed
        # to find it — that's the whole point of the discovery surface).
        deadline = time.time() + 5.0
        found = False
        while time.time() < deadline and not found:
            listed = client.get("/requests").json()["requests"]
            found = any(row["job_id"] == job_id for row in listed)
            if not found:
                time.sleep(0.25)
        assert found, "submitted request never appeared in GET /requests"

    def test_awaiting_approval_card_is_fully_populated(self, client: TestClient) -> None:
        """A P1 security request with a registered automation path must pause
        at the gate and expose a complete approval card."""
        payload = {
            "issue": {
                "key": "SEC-1001",
                "fields": {
                    "summary": "Public s3 bucket in production — block access now",
                    "description": "bucket exposed",
                    "priority": {"name": "Highest"},
                },
            },
            "resource_id": "acme-prod-logs",
        }
        job_id = client.post("/webhooks/jira", json=payload).json()["job_id"]
        body = _poll_request(client, job_id, {"awaiting_approval"})
        card = body["request"]
        assert card["source"] == "jira"
        assert card["external_id"] == "SEC-1001"
        assert card["category"] == "security"
        assert card["platform"] == "aws"
        assert card["priority"] == "P1"
        assert card["risk_level"] in ("high", "critical")
        assert isinstance(card["risk_score"], int)
        assert card["requires_approval"] is True

        approval = body["detail"]["approval_request"]
        assert approval["resume_url"] == f"/requests/{job_id}/approve"
        assert approval["plan"]["steps"], "plan must carry steps"
        assert approval["root_cause"]["summary"]

        # It must also be reachable via the status filter.
        filtered = client.get("/requests", params={"status": "awaiting_approval"}).json()
        assert any(r["job_id"] == job_id for r in filtered["requests"])

    def test_approve_resumes_to_completion(self, client: TestClient) -> None:
        payload = {
            "issue": {
                "key": "SEC-1002",
                "fields": {
                    "summary": "Public s3 bucket in production — block access now",
                    "description": "bucket exposed",
                    "priority": {"name": "Highest"},
                },
            },
            "resource_id": "acme-prod-logs",
        }
        job_id = client.post("/webhooks/jira", json=payload).json()["job_id"]
        _poll_request(client, job_id, {"awaiting_approval"})

        approve = client.post(
            f"/requests/{job_id}/approve",
            json={"approved": True, "approver": "phani", "comment": "go"},
        )
        assert approve.status_code == 202
        body = _poll_request(client, job_id, {"completed"})
        assert body["request"]["workflow_status"] in ("completed", "completed_with_issues")

    def test_approve_wrong_state_conflicts(self, client: TestClient) -> None:
        """Approving a job that is not awaiting approval must 409."""
        job_id = client.post(
            "/requests",
            json={"source": "rest_api", "payload": {"title": "no approval needed here"}},
        ).json()["job_id"]
        _poll_request(client, job_id, {"completed"})
        conflict = client.post(f"/requests/{job_id}/approve", json={"approved": True})
        assert conflict.status_code == 409

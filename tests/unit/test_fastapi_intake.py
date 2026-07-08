"""FastAPI Digital Worker intake surface: webhook auth + health endpoints.

Uses TestClient against the real app object; the Digital Worker jobs are
not executed (submission returns 202 immediately), so no AWS/Bedrock/DB
access happens in these tests beyond app import.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import fastapi_app
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(fastapi_app.app) as test_client:
        yield test_client


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

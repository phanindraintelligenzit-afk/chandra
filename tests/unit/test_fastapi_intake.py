"""FastAPI Digital Worker intake surface: webhook auth + health endpoints.

Uses TestClient against the real app object; the Digital Worker jobs are
not executed (submission returns 202 immediately), so no AWS/Bedrock/DB
access happens in these tests beyond app import.
"""

from __future__ import annotations

import json
import time
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

# fastapi_app is imported lazily below to avoid pulling in the
# digitalworker_agents dependency chain at module-collection time
# (aws_execution_agent.py has a pre-existing syntax error in some
# Python versions that prevents module-level import).


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    import fastapi_app
    from fastapi.testclient import TestClient

    with TestClient(fastapi_app.app) as test_client:
        yield test_client


def _poll_request(
    # 60s ceiling (not 20s): these poll the full Digital Worker graph to a
    # terminal state, and a heavily-loaded CI runner can starve the worker
    # thread well past 20s. Fast jobs still return as soon as they finish —
    # this only raises the timeout, not the normal duration.
    client: TestClient,
    job_id: str,
    wanted: set[str],
    timeout_s: float = 60.0,
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


class TestCustomKraPersistence:
    def test_get_and_put_custom_kras(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        import fastapi_app

        monkeypatch.setattr(fastapi_app, "CUSTOM_KRA_FILE", tmp_path / "customKras.json")

        empty_response = client.get("/customKras")
        assert empty_response.status_code == 200
        assert empty_response.json()["kras"] == []

        payload = {
            "kras": [
                {
                    "name": "Backup Compliance",
                    "description": "Verify backup coverage",
                    "selected": True,
                }
            ]
        }
        save_response = client.put("/customKras", json=payload)
        assert save_response.status_code == 200
        assert save_response.json()["count"] == 1

        read_response = client.get("/customKras")
        assert read_response.status_code == 200
        assert read_response.json()["kras"] == payload["kras"]


class TestGenericJobStatus:
    def test_returns_status_for_stored_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fastapi_app

        job_id = "job-status-test"
        monkeypatch.setitem(
            fastapi_app._job_store,
            job_id,
            {
                "status": "running",
                "progress": 40,
                "message": "Working",
                "result": None,
            },
        )

        response = client.get(f"/jobs/status/{job_id}")

        assert response.status_code == 200
        assert response.json() == {
            "job_id": job_id,
            "status": "running",
            "progress": 40,
            "message": "Working",
            "result": None,
        }

    def test_unknown_job_returns_not_found(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fastapi_app

        monkeypatch.setattr(fastapi_app, "_recover_job_from_disk", lambda _: None)

        response = client.get("/jobs/status/unknown-job-status-test")

        assert response.status_code == 404
        assert response.json()["status"] == "not_found"


class TestSandboxArtifactDownload:
    def test_explicit_job_id_is_not_overridden_by_jira_ticket_mapping(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        import fastapi_app

        requested_job_id = "s3-bucket-job"
        mapped_job_id = "ec2-instance-job"
        worker_dir = tmp_path / "terraform_runs" / "default_worker"
        requested_sandbox = worker_dir / requested_job_id
        mapped_sandbox = worker_dir / mapped_job_id
        requested_sandbox.mkdir(parents=True)
        mapped_sandbox.mkdir(parents=True)
        (requested_sandbox / "bucket-artifact.txt").write_text("S3 artifact", encoding="utf-8")
        (mapped_sandbox / "instance-artifact.txt").write_text("EC2 artifact", encoding="utf-8")

        monkeypatch.setattr(fastapi_app, "__file__", str(tmp_path / "fastapi_app.py"))
        monkeypatch.setitem(fastapi_app._ticket_to_job_id, "DEV-000", mapped_job_id)
        monkeypatch.setattr(fastapi_app, "_build_full_job_logs", lambda _: "")

        response = client.get(
            "/download_sandbox",
            params={"job_id": requested_job_id, "jiraUrl": "DEV-000"},
        )

        assert response.status_code == 200
        archive_path = tmp_path / "requested-artifacts.zip"
        archive_path.write_bytes(response.content)
        with zipfile.ZipFile(archive_path) as archive:
            assert "bucket-artifact.txt" in archive.namelist()
            assert "instance-artifact.txt" not in archive.namelist()

    def test_downloads_files_from_sandbox_as_zip(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        import fastapi_app

        (tmp_path / "main.tf").write_text("terraform {}", encoding="utf-8")
        (tmp_path / ".terraform").mkdir()
        (tmp_path / ".terraform" / "provider.bin").write_text("skip", encoding="utf-8")
        monkeypatch.setattr(fastapi_app, "_build_full_job_logs", lambda _: "")

        response = client.get("/download_sandbox", params={"path": str(tmp_path)})

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"].startswith("attachment;")
        archive_path = tmp_path / "download.zip"
        archive_path.write_bytes(response.content)
        with zipfile.ZipFile(archive_path) as archive:
            assert "main.tf" in archive.namelist()
            assert all(".terraform" not in name for name in archive.namelist())

    def test_missing_sandbox_returns_error_instead_of_placeholder_zip(
        self, client: TestClient
    ) -> None:
        response = client.get(
            "/download_sandbox",
            params={"job_id": "missing-artifact-job-51d4c946"},
        )

        assert response.status_code == 404
        assert "Sandbox directory not found" in response.json()["error"]

    def test_downloads_persisted_job_artifacts_after_sandbox_removal(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        import fastapi_app

        job_id = "destroyed-artifact-job-51d4c946"
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / f"{job_id}.meta.json").write_text(
            json.dumps(
                {
                    "status": "destroyed",
                    "result": {
                        "status": "success",
                        "sandbox_path": "removed",
                    },
                }
            ),
            encoding="utf-8",
        )
        (logs_dir / f"{job_id}.log").write_text("Terraform execution completed.", encoding="utf-8")
        monkeypatch.setattr(fastapi_app, "__file__", str(tmp_path / "fastapi_app.py"))

        response = client.get("/download_sandbox", params={"job_id": job_id})

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        archive_path = tmp_path / "persisted-artifacts.zip"
        archive_path.write_bytes(response.content)
        with zipfile.ZipFile(archive_path) as archive:
            assert "job_metadata.json" in archive.namelist()
            assert "execution_result.json" in archive.namelist()
            assert f"logs/{job_id}.log" in archive.namelist()


class TestSandboxDestroyJiraComment:
    @staticmethod
    def _mock_successful_terraform(monkeypatch: pytest.MonkeyPatch) -> None:
        import fastapi_app

        class CompletedProcess:
            stdout = iter(["terraform destroy completed\n"])
            returncode = 0

            def wait(self) -> None:
                return None

        monkeypatch.setattr(
            fastapi_app.subprocess,
            "Popen",
            lambda *args, **kwargs: CompletedProcess(),
        )

    def test_comments_destroy_outputs_without_deleting_jira_issue(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from src.chandra.digital_worker import tracker
        from tools.jira_tools import create_jira_ticket as jira_module

        self._mock_successful_terraform(monkeypatch)
        monkeypatch.setattr(tracker, "get_active_agent_name", lambda: "RAMESH BHAI")
        (tmp_path / "main.tf").write_text("terraform {}", encoding="utf-8")
        (tmp_path / "execution_result.json").write_text(
            json.dumps(
                {
                    "outputs": {
                        "bucket_name": {"value": "analytics-data-a41f2691"},
                        "bucket_arn": {"value": "arn:aws:s3:::analytics-data-a41f2691"},
                        "bucket_region": {"value": "us-east-1"},
                    }
                }
            ),
            encoding="utf-8",
        )
        comments: list[tuple[str, str]] = []
        deleted_keys: list[str] = []

        def add_comment(issue_key: str, comment: str) -> dict[str, str]:
            comments.append((issue_key, comment))
            return {"status": "success"}

        def record_delete(issue_key: str) -> None:
            deleted_keys.append(issue_key)

        monkeypatch.setattr(
            jira_module,
            "add_summary_comment",
            add_comment,
        )
        monkeypatch.setattr(jira_module, "delete_jira_ticket", record_delete)

        response = client.post(
            "/destroy_sandbox",
            json={
                "path": str(tmp_path),
                "job_id": "destroy-jira-test",
                "jira_issue_key": "DEV-1205",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "success"
        assert response.json()["jira_status"] == "commented"
        assert deleted_keys == []
        assert len(comments) == 1
        issue_key, comment = comments[0]
        assert issue_key == "DEV-1205"
        assert "Infrastructure destroyed successfully." in comment
        assert "RAMESH BHAI outcome: executed (dry_run=False)." in comment
        assert "bucket_name : analytics-data-a41f2691" in comment
        assert "bucket_arn : arn:aws:s3:::analytics-data-a41f2691" in comment
        assert "region : us-east-1" in comment
        assert "Validation passed: false." in comment

    def test_reports_jira_comment_failure(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        self._mock_successful_terraform(monkeypatch)
        (tmp_path / "main.tf").write_text("terraform {}", encoding="utf-8")
        from tools.jira_tools import create_jira_ticket as jira_module

        monkeypatch.setattr(
            jira_module,
            "add_summary_comment",
            lambda _issue_key, _comment: {
                "status": "error",
                "message": "Jira comment permission denied.",
            },
        )

        response = client.post(
            "/destroy_sandbox",
            json={
                "path": str(tmp_path),
                "job_id": "destroy-jira-permission-test",
                "jira_issue_key": "DEV-1205",
            },
        )

        assert response.status_code == 200
        assert response.json()["infrastructure_status"] == "destroyed"
        assert response.json()["jira_status"] == "error"
        assert "comment permission denied" in response.json()["jira_error"]

    def test_includes_lambda_outputs_in_jira_destroy_comment(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from tools.jira_tools import create_jira_ticket as jira_module

        self._mock_successful_terraform(monkeypatch)
        (tmp_path / "main.tf").write_text("terraform {}", encoding="utf-8")
        (tmp_path / "execution_result.json").write_text(
            json.dumps(
                {
                    "outputs": {
                        "function_arn": {
                            "value": (
                                "arn:aws:lambda:us-east-1:123456789012:"
                                "function:worker-func-test"
                            )
                        },
                        "function_name": {"value": "worker-func-test"},
                        "region": {"value": "us-east-1"},
                    }
                }
            ),
            encoding="utf-8",
        )
        comments: list[tuple[str, str]] = []
        monkeypatch.setattr(
            jira_module,
            "add_summary_comment",
            lambda issue_key, comment: (
                comments.append((issue_key, comment)) or {"status": "success"}
            ),
        )

        response = client.post(
            "/destroy_sandbox",
            json={
                "path": str(tmp_path),
                "job_id": "destroy-lambda-comment-test",
                "jira_issue_key": "DEV-1212",
            },
        )

        assert response.status_code == 200
        assert response.json()["jira_status"] == "commented"
        assert len(comments) == 1
        issue_key, comment = comments[0]
        assert issue_key == "DEV-1212"
        assert "function_name : worker-func-test" in comment
        assert (
            "function_arn : arn:aws:lambda:us-east-1:123456789012:"
            "function:worker-func-test"
        ) in comment
        assert "region : us-east-1" in comment
        assert "bucket_name : N/A" not in comment
        assert "bucket_arn : N/A" not in comment


class TestWebhookAuth:
    def test_unknown_source_rejected(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHANDRA_WEBHOOK_TOKEN", raising=False)
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

    def test_awaiting_approval_card_is_fully_populated(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """A P1 security request with a registered automation path must pause
        at the gate and expose a complete approval card."""
        monkeypatch.delenv("CHANDRA_WEBHOOK_TOKEN", raising=False)
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

    def test_approve_resumes_to_completion(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CHANDRA_WEBHOOK_TOKEN", raising=False)
        monkeypatch.setattr(
            "digitalworker_agents.aws_execution_agent.ExecutionAgents.GenerateTerraformOnly",
            lambda *args, **kwargs: {"status": "success", "hcl": "terraform { }\noutput \"fake\" { value = \"1\" }"}
        )
        payload = {
            "dry_run": True,
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
        
        # It should now pause at awaiting_permission
        _poll_request(client, job_id, {"awaiting_permission"})
        
        # Now attach permission set — should proceed through Gate 1 and pause at Gate 2
        attach = client.post(
            f"/requests/{job_id}/approve",
            json={"approved": True, "approver": "Copilot", "permission_set_id": "eab39a74-a48a-4f19-9803-e71e37cc4d62"},
        )
        assert attach.status_code == 202

        _poll_request(client, job_id, {"awaiting_gate2"})

        # Approve Gate 2 to resume to completion
        gate2 = client.post(
            f"/requests/{job_id}/approve",
            json={"approved": True, "approver": "pvr", "comment": "execute"},
        )
        assert gate2.status_code == 202

        body = _poll_request(client, job_id, {"completed", "dry_run"})
        assert body["request"]["workflow_status"] in ("completed", "completed_with_issues")

    def test_approve_wrong_state_conflicts(self, client: TestClient) -> None:
        """Approving a job that is not awaiting approval must 409."""
        job_id = client.post(
            "/requests",
            json={
                "source": "rest_api",
                "payload": {"title": "no approval needed here"},
                "dry_run": True,
            },
        ).json()["job_id"]
        _poll_request(client, job_id, {"completed", "dry_run"})
        conflict = client.post(f"/requests/{job_id}/approve", json={"approved": True})
        assert conflict.status_code == 409

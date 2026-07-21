"""Deterministic executor: dry-run default, typed dispatch, fail-safe gate.

AWS calls are exercised against a fake client factory (no moto needed for
the dispatch logic itself), proving the executor only ever does
``getattr(client, operation)(**parameters)`` and never parses text.
"""

from __future__ import annotations

from typing import Any

from src.chandra.execution.executor import execute_plan
from src.chandra.execution.schemas import (
    AwsApiAction,
    ExecutionPlan,
    ExecutionStep,
    KubernetesAction,
    NoopAction,
    TerraformAction,
)


class _FakeClient:
    def __init__(self, service: str, recorder: list[tuple[str, str, dict[str, Any]]]) -> None:
        self._service = service
        self._recorder = recorder

    def put_public_access_block(self, **kwargs: Any) -> dict[str, Any]:
        self._recorder.append((self._service, "put_public_access_block", kwargs))
        return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Applied": True}


class _FakeFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def client(self, service: str, region: str | None = None) -> _FakeClient:
        return _FakeClient(service, self.calls)


def _aws_plan(dry_run: bool = True) -> ExecutionPlan:
    return ExecutionPlan(
        intent="Block public access on S3 bucket acme-logs",
        kra_code="security",
        dry_run=dry_run,
        steps=[
            ExecutionStep(
                order=1,
                rationale="close public exposure",
                action=AwsApiAction(
                    service="s3",
                    operation="put_public_access_block",
                    resource_id="acme-logs",
                    parameters={"Bucket": "acme-logs"},
                ),
            )
        ],
    )


def test_dry_run_default_does_not_call_aws() -> None:
    factory = _FakeFactory()
    result = execute_plan(_aws_plan(dry_run=True), factory=factory)  # type: ignore[arg-type]
    assert result.executed is True
    assert result.dry_run is True
    assert result.ok is True
    assert factory.calls == []  # nothing was actually invoked
    assert result.steps[0].status == "dry_run"
    assert result.steps[0].output["intended_call"]["operation"] == "put_public_access_block"


def test_plan_dry_run_true_overrides_force_execute() -> None:
    factory = _FakeFactory()
    # Plan says dry_run=True; even with force_execute we must NOT call AWS.
    result = execute_plan(_aws_plan(dry_run=True), factory=factory, force_execute=True)  # type: ignore[arg-type]
    assert result.dry_run is True
    assert factory.calls == []


def test_force_execute_with_plan_opt_out_calls_aws() -> None:
    factory = _FakeFactory()
    result = execute_plan(_aws_plan(dry_run=False), factory=factory, force_execute=True)  # type: ignore[arg-type]
    assert result.dry_run is False
    assert result.executed is True
    assert factory.calls == [("s3", "put_public_access_block", {"Bucket": "acme-logs"})]
    assert result.steps[0].status == "executed"
    # ResponseMetadata is stripped from the recorded output.
    assert "ResponseMetadata" not in result.steps[0].output
    assert result.steps[0].output["Applied"] is True


def test_force_execute_without_plan_opt_out_stays_dry() -> None:
    factory = _FakeFactory()
    # Plan defaults dry_run=True, so force_execute alone is not enough.
    result = execute_plan(_aws_plan(dry_run=True), factory=factory, force_execute=True)  # type: ignore[arg-type]
    assert result.dry_run is True
    assert factory.calls == []


def test_missing_operation_is_failed_not_crash() -> None:
    factory = _FakeFactory()
    plan = ExecutionPlan(
        intent="do something",
        dry_run=False,
        steps=[
            ExecutionStep(
                order=1,
                action=AwsApiAction(
                    service="s3",
                    operation="no_such_operation",
                    resource_id="acme-logs",
                    parameters={},
                ),
            )
        ],
    )
    result = execute_plan(plan, factory=factory, force_execute=True)  # type: ignore[arg-type]
    assert result.steps[0].status == "failed"
    assert result.ok is False
    assert result.errors


def test_noop_is_skipped() -> None:
    plan = ExecutionPlan(
        intent="advisory only",
        steps=[ExecutionStep(order=1, action=NoopAction(note="hand off to engineer"))],
    )
    result = execute_plan(plan, factory=_FakeFactory())  # type: ignore[arg-type]
    assert result.steps[0].status == "skipped"
    assert result.steps[0].detail == "hand off to engineer"


def test_kubernetes_is_unsupported_not_error() -> None:
    plan = ExecutionPlan(
        intent="scale a deployment",
        dry_run=False,
        steps=[
            ExecutionStep(
                order=1,
                action=KubernetesAction(
                    manifest={"apiVersion": "apps/v1", "kind": "Deployment"},
                    namespace="default",
                ),
            )
        ],
    )
    result = execute_plan(plan, factory=_FakeFactory(), force_execute=True)  # type: ignore[arg-type]
    assert result.steps[0].status == "unsupported"


def test_terraform_dry_run_when_binary_absent_is_skipped() -> None:
    plan = ExecutionPlan(
        intent="tag resources via terraform",
        steps=[
            ExecutionStep(
                order=1,
                action=TerraformAction(hcl='resource "null_resource" "x" {}'),
            )
        ],
    )
    result = execute_plan(plan, factory=_FakeFactory())  # type: ignore[arg-type]
    # Either terraform validated (dry_run) or the binary was unavailable
    # (skipped) — never executed against a live backend, never rejected here.
    assert result.steps[0].status in ("dry_run", "skipped")


def test_invalid_plan_is_refused() -> None:
    # A raw dict that fails validation (unknown AWS service) must be refused
    # outright, not executed.
    bad = {
        "intent": "do a thing",
        "dry_run": False,
        "steps": [
            {
                "order": 1,
                "action": {
                    "kind": "aws_api",
                    "service": "totally-made-up-service",
                    "operation": "do_it",
                    "resource_id": "x",
                    "parameters": {},
                },
            }
        ],
    }
    result = execute_plan(bad, factory=_FakeFactory(), force_execute=True)  # type: ignore[arg-type]
    assert result.executed is False
    assert result.ok is False
    assert any("validation" in e for e in result.errors)

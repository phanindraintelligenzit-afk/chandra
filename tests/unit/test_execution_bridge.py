"""Typed request→execution bridge: approval + dry-run contract.

A scripted provider returns canned plan JSON and a fake factory records
AWS calls, so the whole pipeline (plan → validate → verify → execute) is
exercised without a real model or real AWS.
"""

from __future__ import annotations

import json
from typing import Any

from src.chandra.execution.bridge import plan_and_execute
from src.chandra.llm.providers import BaseLLM

_VALID_PLAN = {
    "intent": "Block public access on S3 bucket acme-logs",
    "kra_code": "security",
    "dry_run": True,
    "steps": [
        {
            "order": 1,
            "requires_approval": True,
            "rationale": "close public exposure",
            "action": {
                "kind": "aws_api",
                "service": "s3",
                "operation": "put_public_access_block",
                "resource_id": "acme-logs",
                "parameters": {"Bucket": "acme-logs"},
            },
        }
    ],
}


class ScriptedLLM(BaseLLM):
    provider = "scripted"

    def __init__(self, responses: list[str]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    def _build(self) -> object:  # pragma: no cover - never built
        raise AssertionError("no real model")

    def complete(self, system: str, user: str, **_: object) -> str:
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class _FakeClient:
    def __init__(self, service: str, recorder: list[tuple[str, str]]) -> None:
        self._service = service
        self._recorder = recorder

    def put_public_access_block(self, **_: Any) -> dict[str, Any]:
        self._recorder.append((self._service, "put_public_access_block"))
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _FakeFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def client(self, service: str, region: str | None = None) -> _FakeClient:
        return _FakeClient(service, self.calls)


def test_unapproved_returns_plan_without_executing() -> None:
    factory = _FakeFactory()
    result = plan_and_execute(
        "Block public access on S3 bucket acme-logs",
        llm=ScriptedLLM([json.dumps(_VALID_PLAN)]),
        factory=factory,  # type: ignore[arg-type]
        approved=False,
    )
    assert result.planned is True
    assert result.executed is False
    assert result.execution is None
    assert factory.calls == []  # approval gate: nothing ran


def test_approved_dry_run_does_not_call_aws() -> None:
    factory = _FakeFactory()
    result = plan_and_execute(
        "Block public access on S3 bucket acme-logs",
        llm=ScriptedLLM([json.dumps(_VALID_PLAN)]),
        factory=factory,  # type: ignore[arg-type]
        approved=True,
        dry_run=True,
    )
    assert result.executed is True
    assert result.dry_run is True
    assert result.ok is True
    assert factory.calls == []
    assert result.execution is not None
    assert result.execution.steps[0].status == "dry_run"


def test_approved_real_execution_calls_aws() -> None:
    factory = _FakeFactory()
    result = plan_and_execute(
        "Block public access on S3 bucket acme-logs",
        llm=ScriptedLLM([json.dumps(_VALID_PLAN)]),
        factory=factory,  # type: ignore[arg-type]
        approved=True,
        dry_run=False,
    )
    assert result.executed is True
    assert result.dry_run is False
    assert result.ok is True
    assert factory.calls == [("s3", "put_public_access_block")]
    assert result.plan.dry_run is False  # plan opted out for the real run


def test_invalid_plan_never_executes() -> None:
    factory = _FakeFactory()
    result = plan_and_execute(
        "do a thing",
        llm=ScriptedLLM(["not json", "still not", "nope"]),
        factory=factory,  # type: ignore[arg-type]
        approved=True,
        dry_run=False,
    )
    assert result.planned is False
    assert result.executed is False
    assert result.execution is None
    assert factory.calls == []
    # The returned guidance plan is a safe no-op.
    assert result.plan.generated_by == "deterministic"

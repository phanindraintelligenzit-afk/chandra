"""Structured-JSON planner with self-correction.

The provider is a fake returning canned strings, so we test the
generate → validate → (verify) → correct → fallback control flow without a
real model.
"""

from __future__ import annotations

import json

from src.chandra.execution.planner import PlanGenerationResult, generate_execution_plan
from src.chandra.execution.schemas import ActionKind
from src.chandra.llm.providers import BaseLLM


class ScriptedLLM(BaseLLM):
    """A BaseLLM whose ``complete`` returns canned strings in order.

    Records the prompts it was given so self-correction (feeding rejection
    reasons back in) can be asserted.
    """

    provider = "scripted"

    def __init__(self, responses: list[str]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls = 0
        self.prompts: list[str] = []

    def _build(self) -> object:  # pragma: no cover - never built
        raise AssertionError("ScriptedLLM does not build a real model")

    def complete(self, system: str, user: str, **_: object) -> str:
        self.prompts.append(user)
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


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


def test_valid_first_attempt() -> None:
    llm = ScriptedLLM([json.dumps(_VALID_PLAN)])
    result = generate_execution_plan(
        "Block public access on S3 bucket acme-logs", llm=llm, kra_code="security"
    )
    assert isinstance(result, PlanGenerationResult)
    assert result.valid is True
    assert result.attempts == 1
    assert result.plan.steps[0].action.kind is ActionKind.AWS_API
    assert llm.calls == 1


def test_self_correction_after_bad_json() -> None:
    llm = ScriptedLLM(["this is not json at all", json.dumps(_VALID_PLAN)])
    result = generate_execution_plan(
        "Block public access on S3 bucket acme-logs", llm=llm, kra_code="security"
    )
    assert result.valid is True
    assert result.attempts == 2
    assert llm.calls == 2
    # The rejection reason must have been fed back into the second prompt.
    assert "REJECTED" in llm.prompts[1]


def test_exhausts_attempts_returns_deterministic_fallback() -> None:
    llm = ScriptedLLM(["nope", "still nope", "nope again"])
    result = generate_execution_plan("do a thing", llm=llm, max_attempts=3)
    assert result.valid is False
    assert result.generated_by == "deterministic"
    assert result.plan.generated_by == "deterministic"
    assert result.plan.steps[0].action.kind is ActionKind.NOOP
    assert result.attempts == 3
    assert llm.calls == 3


def test_provider_exception_falls_back(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Boom(ScriptedLLM):
        def complete(self, system: str, user: str, **_: object) -> str:
            raise RuntimeError("backend down")

    result = generate_execution_plan("x", llm=Boom([]))
    assert result.valid is False
    assert result.plan.steps[0].action.kind is ActionKind.NOOP


def test_off_intent_destructive_plan_is_rejected_and_corrected() -> None:
    # First: a schema-valid plan that deletes a bucket the request never
    # asked to delete → intent verification fails → self-correct.
    destructive = {
        "intent": "secure the acme-logs bucket",
        "dry_run": True,
        "steps": [
            {
                "order": 1,
                "rationale": "delete it",
                "action": {
                    "kind": "aws_api",
                    "service": "s3",
                    "operation": "delete_bucket",
                    "resource_id": "acme-logs",
                    "parameters": {"Bucket": "acme-logs"},
                },
            }
        ],
    }
    llm = ScriptedLLM([json.dumps(destructive), json.dumps(_VALID_PLAN)])
    # Non-destructive request intent.
    result = generate_execution_plan("secure the acme-logs bucket", llm=llm)
    assert result.valid is True
    assert result.attempts == 2

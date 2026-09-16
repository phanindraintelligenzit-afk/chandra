"""Memory is never an authorization source (PRD §26.6, stated three times).

A cached, previously-verified plan must still traverse every current check:
risk scoring, the approval decision, Gate 1 permission verification and Gate 2
human review. Reusing a plan skips the *planning* work, never the *governance*.

These are the load-bearing tests for the PRD's core stance. If one of them
starts failing because a memory hit short-circuits a gate, that is a governance
regression, not a flaky test — do not "fix" it by relaxing the assertion.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.chandra.db.models import Base
from src.chandra.digital_worker import graph as dw_graph
from src.chandra.digital_worker import memory, planner
from src.chandra.digital_worker.graph import build_digital_worker_graph
from src.chandra.digital_worker.schemas import ResolutionPlan, ResolutionStep

from tests.conftest import seed_permission_sets

S3_FULL_ACCESS_PSET_ID = "eab39a74-a48a-4f19-9803-e71e37cc4d62"

# A destructive AWS change: scores high enough to require approval, and needs
# permissions Gate 1 has to verify. Exactly the case where skipping a gate on a
# cache hit would be catastrophic.
JIRA_PAYLOAD: dict[str, Any] = {
    "source": "jira",
    "dry_run": True,
    "payload": {
        "key": "OPS-4242",
        "fields": {
            "summary": "Public S3 bucket in production — block access now",
            "description": "Bucket acme-prod-assets is world-readable. Block public access.",
        },
    },
}

THREAD_1 = {"configurable": {"thread_id": "memory-gate-1"}}


def _cached_plan() -> ResolutionPlan:
    """A plan as it comes back from resolution_memory: verified, reused, trusted
    for *content* only."""
    return ResolutionPlan(
        summary="Block public access on the offending bucket",
        steps=[
            ResolutionStep(
                order=1,
                action="Enable S3 Block Public Access",
                detail="Apply the account-level public access block",
                expected_outcome="Bucket no longer world-readable",
            )
        ],
        generated_by="memory",
        fingerprint="cached-fingerprint-abc",
    )


@pytest.fixture
def scope() -> Iterator[Any]:
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    yield _scope


@pytest.fixture
def memory_hit_workflow(monkeypatch: pytest.MonkeyPatch, scope: Any) -> Any:
    """Graph where every request is a memory cache hit.

    ``compose_request_analysis`` is made to raise: if anything ever reaches the
    LLM planning route, the test fails loudly instead of silently passing for
    the wrong reason.
    """

    def _no_llm(payload: Any) -> Any:
        raise AssertionError("LLM planning route taken — the memory hit did not apply")

    monkeypatch.setattr(memory, "lookup_plan", lambda fingerprint: _cached_plan())
    monkeypatch.setattr(planner.memory, "lookup_plan", lambda fingerprint: _cached_plan())
    monkeypatch.setattr(planner, "compose_request_analysis", _no_llm)
    monkeypatch.setattr(dw_graph, "session_scope", scope)
    for var in ("JIRA_SERVER", "JIRA_EMAIL", "JIRA_API_TOKEN", "SLACK_WEBHOOK_URL", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CHANDRA_TERRAFORM_APPLY_ENABLED", raising=False)
    monkeypatch.setattr(
        "digitalworker_agents.aws_execution_agent.ExecutionAgents.GenerateTerraformOnly",
        lambda *a, **k: {"status": "success", "hcl": 'terraform { }\noutput "o" { value = "1" }'},
    )
    seed_permission_sets(scope)
    return build_digital_worker_graph(checkpointer=MemorySaver())


def _events(state: dict[str, Any]) -> list[str]:
    return [e.event for e in state.get("audit_trail", [])]


class TestMemoryHitTraversesGateChain:
    def test_cached_plan_is_actually_used(self, memory_hit_workflow: Any) -> None:
        """Guard for the tests below: prove the run really is a cache hit."""
        state = memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=THREAD_1)
        assert state["plan"].generated_by == "memory"

    def test_risk_is_scored_fresh_on_a_cache_hit(self, memory_hit_workflow: Any) -> None:
        """A cached plan does not carry a cached risk verdict."""
        state = memory_hit_workflow.invoke(
            dict(JIRA_PAYLOAD), config={"configurable": {"thread_id": "memory-gate-risk"}}
        )
        assert state["risk"] is not None
        assert state["risk"].score > 0
        assert "risk_assessed" in _events(state)

    def test_approval_decision_is_made_on_a_cache_hit(self, memory_hit_workflow: Any) -> None:
        """The approval decision is re-derived; a memory hit is not pre-approved."""
        state = memory_hit_workflow.invoke(
            dict(JIRA_PAYLOAD), config={"configurable": {"thread_id": "memory-gate-decision"}}
        )
        assert state["decision"] is not None
        assert "decision_made" in _events(state)
        assert state["decision"].mode.value == "await_approval"

    def test_cache_hit_halts_at_the_human_approval_gate(self, memory_hit_workflow: Any) -> None:
        """The run must not reach execution without a human. This is the one
        that matters: a cached plan executing unattended is the failure mode the
        PRD's stance exists to prevent."""
        config = {"configurable": {"thread_id": "memory-gate-halt"}}
        memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)
        snapshot = memory_hit_workflow.get_state(config)

        assert snapshot.next, "workflow ran to completion without pausing for approval"
        assert "approval_gate" in snapshot.next
        events = _events(snapshot.values)
        assert "terraform_applied" not in events
        assert "automation_executed" not in events

    def test_cache_hit_still_requires_a_permission_set_at_gate_1(
        self, memory_hit_workflow: Any
    ) -> None:
        """After human approval, a cached plan still enters permission analysis
        and stops for a permission set — it does not inherit one from memory."""
        config = {"configurable": {"thread_id": "memory-gate-perms"}}
        memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)
        memory_hit_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=config,
        )
        snapshot = memory_hit_workflow.get_state(config)
        values = snapshot.values

        assert values.get("required_permissions"), "permission analysis was skipped on a cache hit"
        assert values.get("gate_1_passed") is not True
        assert "permission_selection_pause" in (snapshot.next or ())

    def test_cache_hit_runs_gate_1_verification_against_live_policy(
        self, memory_hit_workflow: Any
    ) -> None:
        """Gate 1 evaluates the attached permission set against the *current*
        catalogue in Postgres, not against anything stored with the plan."""
        config = {"configurable": {"thread_id": "memory-gate-g1"}}
        memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)
        memory_hit_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=config,
        )
        memory_hit_workflow.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=config,
        )
        values = memory_hit_workflow.get_state(config).values

        assert values.get("gate_1_result") is not None, "Gate 1 did not run on a cache hit"
        assert {"gate_1_passed", "gate_1_denied"} & set(_events(values))

    def test_gate_1_denies_a_cached_plan_when_policy_no_longer_allows_it(
        self, memory_hit_workflow: Any, scope: Any
    ) -> None:
        """The decisive case: the plan succeeded before and is in memory, but the
        permission set no longer grants what it needs. Memory must not rescue it."""
        from src.chandra.catalog import ConfigRepository

        # Current policy: the attached set now grants read-only S3.
        ConfigRepository(session_factory=scope).replace_permission_sets(
            [
                {
                    "id": S3_FULL_ACCESS_PSET_ID,
                    "name": "S3 read-only (tightened)",
                    "aws_service": "s3",
                    "actions": ["s3:GetObject", "s3:ListBucket"],
                }
            ]
        )

        config = {"configurable": {"thread_id": "memory-gate-denied"}}
        memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)
        memory_hit_workflow.invoke(
            Command(resume={"approved": True, "approver": "phani", "comment": "go"}),
            config=config,
        )
        memory_hit_workflow.invoke(
            Command(resume={"permission_set_id": S3_FULL_ACCESS_PSET_ID}),
            config=config,
        )
        values = memory_hit_workflow.get_state(config).values

        assert values.get("gate_1_passed") is False
        assert values["gate_1_result"]["missing_actions"]
        assert "terraform_applied" not in _events(values)


class TestPolicyAppliesToCachedPlans:
    """Stage 6 policy controls bind a reused plan exactly as they bind a fresh one."""

    def _deny_all_aws(self, scope: Any) -> None:
        from src.chandra.db.models import PolicyRuleRecord
        from src.chandra.governance import PolicyEffect, PolicyRule
        from src.chandra.governance.policy import rule_to_criteria

        rule = PolicyRule(
            id="deny-prod-s3",
            name="No automated S3 policy changes in production",
            effect=PolicyEffect.DENY,
            reason="S3 public-access changes are handled by the platform team",
            platforms=["aws"],
        )
        with scope() as session:
            session.add(
                PolicyRuleRecord(
                    id=rule.id,
                    tenant_id="default",
                    name=rule.name,
                    effect=rule.effect.value,
                    priority=rule.priority,
                    enabled=rule.enabled,
                    reason=rule.reason,
                    criteria_jsonb=rule_to_criteria(rule),
                )
            )

    def test_policy_denies_a_cached_plan_and_blocks_execution(
        self, memory_hit_workflow: Any, scope: Any
    ) -> None:
        self._deny_all_aws(scope)
        config = {"configurable": {"thread_id": "memory-policy-deny"}}
        state = memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)

        assert state["plan"].generated_by == "memory"
        assert state["policy_decision"].allowed is False
        assert state["decision"].mode.value == "engineer_guidance"
        events = _events(state)
        assert "policy_denied" in events
        assert "terraform_applied" not in events
        assert "automation_executed" not in events

    def test_denied_run_never_pauses_for_an_approval_that_could_override(
        self, memory_hit_workflow: Any, scope: Any
    ) -> None:
        """A policy denial is not an approval question. The run must not stop at
        the human gate, because stopping there would invite someone to approve
        past a prohibition."""
        self._deny_all_aws(scope)
        config = {"configurable": {"thread_id": "memory-policy-no-gate"}}
        memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)
        snapshot = memory_hit_workflow.get_state(config)
        assert not snapshot.next
        assert "approval_granted" not in _events(snapshot.values)

    def test_policy_is_evaluated_before_risk(self, memory_hit_workflow: Any) -> None:
        """PRD §26.7 ordering: policy controls precede risk analysis."""
        state = memory_hit_workflow.invoke(
            dict(JIRA_PAYLOAD), config={"configurable": {"thread_id": "memory-policy-order"}}
        )
        events = _events(state)
        assert events.index("policy_allowed") < events.index("risk_assessed")

    def test_allowed_run_is_unchanged_by_an_empty_rule_set(self, memory_hit_workflow: Any) -> None:
        """No rules authored => default allow => the pre-policy behaviour of
        halting at the human approval gate is preserved."""
        config = {"configurable": {"thread_id": "memory-policy-default"}}
        memory_hit_workflow.invoke(dict(JIRA_PAYLOAD), config=config)
        snapshot = memory_hit_workflow.get_state(config)
        assert snapshot.values["policy_decision"].default_applied is True
        assert "approval_gate" in (snapshot.next or ())

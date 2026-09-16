"""Policy engine: deterministic allow/deny evaluation (PRD L2 stage 6, §26.7)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.chandra.db.models import Base, PolicyRuleRecord
from src.chandra.governance import (
    PolicyContext,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    PolicyRulesUnavailableError,
)
from src.chandra.governance.policy import rule_to_criteria


def _rule(rule_id: str, effect: str = "deny", **kw: Any) -> PolicyRule:
    return PolicyRule(id=rule_id, name=kw.pop("name", rule_id), effect=PolicyEffect(effect), **kw)


def _ctx(**kw: Any) -> PolicyContext:
    return PolicyContext(**kw)


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


class TestMatching:
    def test_empty_criteria_matches_everything(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny")])
        assert engine.evaluate(_ctx(category="incident")).effect is PolicyEffect.DENY
        assert engine.evaluate(_ctx()).effect is PolicyEffect.DENY

    def test_criteria_are_anded(self) -> None:
        engine = PolicyEngine(
            rules=[_rule("r", "deny", categories=["security"], platforms=["aws"])]
        )
        assert (
            engine.evaluate(_ctx(category="security", platform="aws")).effect is PolicyEffect.DENY
        )
        # one criterion mismatching is enough to make the rule inapplicable
        assert engine.evaluate(_ctx(category="security", platform="azure")).allowed

    def test_values_within_a_criterion_are_ored(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny", platforms=["aws", "azure"])])
        assert engine.evaluate(_ctx(platform="azure")).effect is PolicyEffect.DENY
        assert engine.evaluate(_ctx(platform="gcp")).allowed

    def test_matching_is_case_insensitive(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny", categories=["Security"])])
        assert engine.evaluate(_ctx(category="SECURITY")).effect is PolicyEffect.DENY

    def test_a_constrained_attribute_absent_from_context_does_not_match(self) -> None:
        """A rule about platforms cannot fire on a request with no platform —
        matching an unknown as if it were a hit would deny (or allow) blindly."""
        engine = PolicyEngine(rules=[_rule("r", "deny", platforms=["aws"])])
        assert engine.evaluate(_ctx(category="incident")).allowed

    def test_action_globs(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny", actions=["ec2:Terminate*"])])
        decision = engine.evaluate(
            _ctx(actions=["ec2:DescribeInstances", "ec2:TerminateInstances"])
        )
        assert decision.effect is PolicyEffect.DENY
        assert decision.matched_actions == ["ec2:TerminateInstances"]

    def test_action_rule_does_not_fire_when_no_action_matches(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny", actions=["iam:*"])])
        assert engine.evaluate(_ctx(actions=["s3:GetObject"])).allowed

    def test_task_glob(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny", tasks=["*delete*"])])
        assert engine.evaluate(_ctx(task="Delete RDS snapshot")).effect is PolicyEffect.DENY
        assert engine.evaluate(_ctx(task="Create RDS snapshot")).allowed

    def test_disabled_rule_never_applies(self) -> None:
        engine = PolicyEngine(rules=[_rule("r", "deny", enabled=False)])
        assert engine.evaluate(_ctx()).allowed


class TestResolution:
    def test_deny_beats_allow_regardless_of_priority(self) -> None:
        """The property that makes a deny a prohibition rather than a preference:
        it must not be defeatable by authoring a higher-priority allow."""
        engine = PolicyEngine(
            rules=[
                _rule("allow-all", "allow", priority=999),
                _rule("deny-iam", "deny", priority=1, actions=["iam:*"]),
            ]
        )
        decision = engine.evaluate(_ctx(actions=["iam:CreateUser"]))
        assert decision.effect is PolicyEffect.DENY
        assert decision.matched_rule_id == "deny-iam"

    def test_highest_priority_deny_is_reported(self) -> None:
        engine = PolicyEngine(
            rules=[
                _rule("low", "deny", priority=10, reason="low priority deny"),
                _rule("high", "deny", priority=500, reason="high priority deny"),
            ]
        )
        assert engine.evaluate(_ctx()).matched_rule_id == "high"

    def test_highest_priority_allow_wins_when_no_deny_applies(self) -> None:
        engine = PolicyEngine(
            rules=[
                _rule("broad", "allow", priority=10),
                _rule("specific", "allow", priority=900, categories=["cost_optimization"]),
            ]
        )
        assert engine.evaluate(_ctx(category="cost_optimization")).matched_rule_id == "specific"

    def test_default_applies_when_nothing_matches(self) -> None:
        decision = PolicyEngine(rules=[_rule("r", "deny", platforms=["gcp"])]).evaluate(
            _ctx(platform="aws")
        )
        assert decision.allowed
        assert decision.default_applied is True
        assert decision.matched_rule_id is None

    def test_default_deny_tenant(self) -> None:
        engine = PolicyEngine(rules=[], default_effect=PolicyEffect.DENY)
        decision = engine.evaluate(_ctx(platform="aws"))
        assert decision.effect is PolicyEffect.DENY
        assert decision.default_applied is True

    def test_decision_carries_evidence(self) -> None:
        engine = PolicyEngine(
            rules=[_rule("r1", "deny", reason="Production IAM changes are not automated")]
        )
        decision = engine.evaluate(_ctx())
        assert decision.reason == "Production IAM changes are not automated"
        assert decision.matched_rule_name == "r1"
        assert decision.evaluated_rule_count == 1

    def test_evaluation_is_order_independent(self) -> None:
        rules = [
            _rule("a", "allow", priority=50),
            _rule("d", "deny", priority=20, categories=["security"]),
            _rule("b", "allow", priority=80),
        ]
        ctx = _ctx(category="security")
        first = PolicyEngine(rules=list(rules)).evaluate(ctx)
        second = PolicyEngine(rules=list(reversed(rules))).evaluate(ctx)
        assert first.effect is second.effect is PolicyEffect.DENY
        assert first.matched_rule_id == second.matched_rule_id


class TestPersistence:
    def _store(self, scope: Any, rule: PolicyRule, tenant: str = "default") -> None:
        with scope() as session:
            session.add(
                PolicyRuleRecord(
                    id=rule.id,
                    tenant_id=tenant,
                    name=rule.name,
                    effect=rule.effect.value,
                    priority=rule.priority,
                    enabled=rule.enabled,
                    reason=rule.reason,
                    criteria_jsonb=rule_to_criteria(rule),
                )
            )

    def test_rules_round_trip_through_postgres(self, scope: Any) -> None:
        self._store(
            scope,
            _rule("deny-terminate", "deny", actions=["ec2:Terminate*"], reason="no terminations"),
        )
        engine = PolicyEngine(session_factory=scope)
        decision = engine.evaluate(_ctx(actions=["ec2:TerminateInstances"]))
        assert decision.effect is PolicyEffect.DENY
        assert decision.reason == "no terminations"

    def test_rules_are_tenant_scoped(self, scope: Any) -> None:
        self._store(scope, _rule("deny-all", "deny"), tenant="acme")
        assert PolicyEngine(tenant_id="acme", session_factory=scope).evaluate(_ctx()).effect is (
            PolicyEffect.DENY
        )
        assert PolicyEngine(tenant_id="other", session_factory=scope).evaluate(_ctx()).allowed

    def test_unreadable_rule_set_raises_rather_than_allowing(self) -> None:
        """An unavailable rule set must never degrade to 'no rules, allow all'.
        Memory and caches may fail soft; authorization may not."""

        @contextmanager
        def _broken() -> Iterator[Session]:
            raise SQLAlchemyError("connection refused")
            yield  # pragma: no cover

        engine = PolicyEngine(session_factory=_broken)
        with pytest.raises(PolicyRulesUnavailableError):
            engine.evaluate(_ctx())

    def test_rules_are_read_once_per_engine(self, scope: Any) -> None:
        self._store(scope, _rule("r", "deny"))
        engine = PolicyEngine(session_factory=scope)
        assert engine.evaluate(_ctx()).effect is PolicyEffect.DENY
        with scope() as session:
            session.query(PolicyRuleRecord).delete()
        # cached for the lifetime of this engine: one evaluation per request must
        # not see the rule set change mid-workflow
        assert engine.evaluate(_ctx()).effect is PolicyEffect.DENY
        assert PolicyEngine(session_factory=scope).evaluate(_ctx()).allowed

"""Policy engine — deterministic allow/deny evaluation (PRD L2 stage 6, §26.7).

Distinct from risk scoring, and evaluated before it. Risk answers *how serious
is this*; policy answers *is this permitted at all*. A high-risk action can be
policy-allowed (and then require approval); a low-risk action can be
policy-denied outright.

Position in the control chain (§26.7):

    Identity -> Authentication -> RBAC -> Request Authorization
      -> POLICY CONTROLS      <- this module
      -> Risk Analysis
      -> Permission Check (Gate 1)
      -> HITL Approval if required
      -> Scoped execution role

No LLM is involved at any point. Evaluation is a pure function of the rule set
and the request context, so the same inputs always produce the same verdict and
the verdict is explainable from the matched rule alone.

Rules live in Postgres (``policy_rules``), tenant-scoped, and are configuration
— never inferred, never written by the workflow.
"""

from __future__ import annotations

import fnmatch
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from src.chandra.catalog.repository import DEFAULT_TENANT, SessionFactory
from src.chandra.db.models import PolicyRuleRecord
from src.chandra.db.session import session_scope as _default_session_scope
from src.chandra.logging import get_logger

logger = get_logger(__name__)


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyRule(BaseModel):
    """One allow/deny rule.

    A criterion left empty matches anything. A criterion with values matches when
    the request's value is in the list (case-insensitive; ``task`` and ``action``
    accept glob patterns, e.g. ``ec2:Terminate*``). All populated criteria must
    match for the rule to apply — criteria are ANDed, values within one are ORed.
    """

    id: str
    name: str
    effect: PolicyEffect
    priority: int = 100
    enabled: bool = True
    reason: str = ""

    categories: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    responsibility_areas: list[str] = Field(default_factory=list)
    maturity_levels: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    risk_levels: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)


class PolicyContext(BaseModel):
    """What the engine evaluates a request against. Everything is optional: an
    absent attribute simply cannot satisfy a rule that constrains it."""

    category: str | None = None
    platform: str | None = None
    responsibility_area: str | None = None
    maturity_level: str | None = None
    source: str | None = None
    risk_level: str | None = None
    task: str | None = None
    actions: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    """Verdict plus the evidence for it. Written to the audit trail verbatim."""

    effect: PolicyEffect
    reason: str
    matched_rule_id: str | None = None
    matched_rule_name: str | None = None
    matched_actions: list[str] = Field(default_factory=list)
    evaluated_rule_count: int = 0
    default_applied: bool = False

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW


def _matches_values(rule_values: list[str], value: str | None) -> bool:
    if not rule_values:
        return True
    if value is None:
        return False
    return value.lower() in {v.lower() for v in rule_values}


def _matches_patterns(patterns: list[str], value: str | None) -> bool:
    if not patterns:
        return True
    if value is None:
        return False
    return any(fnmatch.fnmatch(value.lower(), p.lower()) for p in patterns)


def _matching_actions(patterns: list[str], actions: list[str]) -> list[str] | None:
    """Return the actions a rule's action patterns match.

    ``None`` means the rule constrains actions and none matched (rule does not
    apply). An empty list means the rule does not constrain actions at all.
    """
    if not patterns:
        return []
    hits = [a for a in actions if any(fnmatch.fnmatch(a.lower(), p.lower()) for p in patterns)]
    return hits or None


def rule_applies(rule: PolicyRule, context: PolicyContext) -> tuple[bool, list[str]]:
    """``(applies, matched_actions)``. Pure, side-effect free, order independent."""
    if not rule.enabled:
        return False, []

    exact = (
        (rule.categories, context.category),
        (rule.platforms, context.platform),
        (rule.responsibility_areas, context.responsibility_area),
        (rule.maturity_levels, context.maturity_level),
        (rule.sources, context.source),
        (rule.risk_levels, context.risk_level),
    )
    if not all(_matches_values(values, value) for values, value in exact):
        return False, []
    if not _matches_patterns(rule.tasks, context.task):
        return False, []

    matched_actions = _matching_actions(rule.actions, context.actions)
    if matched_actions is None:
        return False, []
    return True, matched_actions


class PolicyEngine:
    """Evaluates a request against the tenant's rule set.

    Resolution order, fixed and deliberate:

    1. **Any applicable DENY wins**, whatever its priority. A deny is a
       prohibition, not a preference — it must not be defeatable by adding a
       higher-priority allow. The highest-priority deny is reported as the
       matched rule.
    2. Otherwise the highest-priority applicable ALLOW decides.
    3. Otherwise ``default_effect`` applies.

    ``default_effect`` is ALLOW: before this engine existed every request was
    permitted, so defaulting to DENY would silently break every deployment on
    upgrade. Whether a tenant should run default-deny once its rule set is
    authored is a business decision, not one to make here — it is a constructor
    argument so it can be flipped per tenant without code changes.
    """

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT,
        session_factory: SessionFactory | None = None,
        rules: list[PolicyRule] | None = None,
        default_effect: PolicyEffect = PolicyEffect.ALLOW,
    ) -> None:
        self.tenant_id = tenant_id
        self._session_factory = session_factory
        self._rules = rules
        self.default_effect = default_effect

    @property
    def rules(self) -> list[PolicyRule]:
        if self._rules is None:
            self._rules = self._load_rules()
        return self._rules

    def _load_rules(self) -> list[PolicyRule]:
        scope = self._session_factory or _default_session_scope
        try:
            with scope() as session:
                rows = session.scalars(
                    select(PolicyRuleRecord)
                    .where(PolicyRuleRecord.tenant_id == self.tenant_id)
                    .order_by(PolicyRuleRecord.priority.desc(), PolicyRuleRecord.id)
                ).all()
                return [_row_to_rule(r) for r in rows]
        except SQLAlchemyError as exc:
            # Fail closed on the rules themselves: an unreadable rule set must not
            # silently become "no rules, everything allowed".
            logger.error("policy.rules_unavailable", tenant=self.tenant_id, error=str(exc))
            raise PolicyRulesUnavailableError(str(exc)) from exc

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        rules = self.rules
        applicable: list[tuple[PolicyRule, list[str]]] = []
        for rule in rules:
            applies, matched_actions = rule_applies(rule, context)
            if applies:
                applicable.append((rule, matched_actions))

        denies = [(r, a) for r, a in applicable if r.effect is PolicyEffect.DENY]
        if denies:
            rule, actions = max(denies, key=lambda pair: pair[0].priority)
            return PolicyDecision(
                effect=PolicyEffect.DENY,
                reason=rule.reason or f"Denied by policy rule '{rule.name}'",
                matched_rule_id=rule.id,
                matched_rule_name=rule.name,
                matched_actions=actions,
                evaluated_rule_count=len(rules),
            )

        allows = [(r, a) for r, a in applicable if r.effect is PolicyEffect.ALLOW]
        if allows:
            rule, actions = max(allows, key=lambda pair: pair[0].priority)
            return PolicyDecision(
                effect=PolicyEffect.ALLOW,
                reason=rule.reason or f"Allowed by policy rule '{rule.name}'",
                matched_rule_id=rule.id,
                matched_rule_name=rule.name,
                matched_actions=actions,
                evaluated_rule_count=len(rules),
            )

        return PolicyDecision(
            effect=self.default_effect,
            reason=f"No policy rule matched; tenant default is {self.default_effect.value}",
            evaluated_rule_count=len(rules),
            default_applied=True,
        )


class PolicyRulesUnavailableError(RuntimeError):
    """The rule set could not be read. Callers must treat this as a hard stop,
    never as an empty rule set."""


def _row_to_rule(row: PolicyRuleRecord) -> PolicyRule:
    criteria: dict[str, Any] = dict(row.criteria_jsonb or {})
    return PolicyRule(
        id=row.id,
        name=row.name,
        effect=PolicyEffect(row.effect),
        priority=row.priority,
        enabled=row.enabled,
        reason=row.reason,
        categories=list(criteria.get("categories", [])),
        platforms=list(criteria.get("platforms", [])),
        responsibility_areas=list(criteria.get("responsibility_areas", [])),
        maturity_levels=list(criteria.get("maturity_levels", [])),
        sources=list(criteria.get("sources", [])),
        risk_levels=list(criteria.get("risk_levels", [])),
        tasks=list(criteria.get("tasks", [])),
        actions=list(criteria.get("actions", [])),
    )


def rule_to_criteria(rule: PolicyRule) -> dict[str, Any]:
    """Inverse of ``_row_to_rule``'s criteria unpacking, for persistence."""
    return {
        "categories": rule.categories,
        "platforms": rule.platforms,
        "responsibility_areas": rule.responsibility_areas,
        "maturity_levels": rule.maturity_levels,
        "sources": rule.sources,
        "risk_levels": rule.risk_levels,
        "tasks": rule.tasks,
        "actions": rule.actions,
    }

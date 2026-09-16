"""Governance layer: policy evaluation and RBAC (PRD L2 stage 6, §26.7).

Deterministic and LLM-free by construction — these decide whether work is
permitted, so they must be explainable and reproducible.
"""

from src.chandra.governance.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    PolicyRulesUnavailableError,
)
from src.chandra.governance.rbac import (
    ROLE_CAPABILITIES,
    AuthorizationError,
    Capability,
    Principal,
    RbacEngine,
    Role,
    RolesUnavailableError,
)

__all__ = [
    "ROLE_CAPABILITIES",
    "AuthorizationError",
    "Capability",
    "PolicyContext",
    "PolicyDecision",
    "PolicyEffect",
    "PolicyEngine",
    "PolicyRule",
    "PolicyRulesUnavailableError",
    "Principal",
    "RbacEngine",
    "Role",
    "RolesUnavailableError",
]

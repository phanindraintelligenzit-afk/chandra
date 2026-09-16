"""JWT RS256 authentication at the FastAPI edge (PRD L2 stage 2, §26.7).

This is the layer RBAC has been waiting for. Until now ``principal_id`` and
``tenant_id`` were caller-asserted headers — the authorization model was real,
the identity was not. Here the same two values are lifted from a **verified**
token, so a capability check finally means something.

Design constraints that are not negotiable:

* **RS256 only.** The algorithm is pinned at verification time. Accepting the
  token's own ``alg`` header is the classic JWT forgery: a token signed
  ``HS256`` with the *public* key as the HMAC secret verifies as valid against a
  naive implementation, and ``alg: none`` skips verification entirely. Both are
  rejected here by construction, and there are tests for each.
* **Public key only.** This process verifies; it never signs. There is no
  private key in the service configuration, so a compromised API cannot mint
  tokens.
* **Expiry, issuer and audience are all verified** when configured. An
  unexpired-forever token is a permanent credential.

Enforcement is opt-in per deployment via ``CHANDRA_AUTH_REQUIRED``. When auth is
not configured the edge falls back to the header-based principal of Phase 2, so
an existing deployment keeps working until its operator turns auth on — the same
activate-by-configuring approach used for RBAC. ``auth_required=true`` with no
public key configured is a hard startup-time error rather than a silent
fallback: "I turned auth on" must never quietly mean "auth is off".
"""

from __future__ import annotations

from typing import Any

import jwt
from pydantic import BaseModel
from src.chandra.catalog.repository import DEFAULT_TENANT
from src.chandra.config import settings
from src.chandra.logging import get_logger

logger = get_logger(__name__)

ALGORITHM = "RS256"

# Claims carrying the principal, in preference order. ``sub`` is the standard.
_PRINCIPAL_CLAIMS = ("sub", "principal_id", "preferred_username", "email")
_TENANT_CLAIMS = ("tenant_id", "tid", "org_id")
_ROLE_CLAIMS = ("roles", "chandra_roles", "groups")


class AuthError(Exception):
    """Token missing, malformed, expired or otherwise unverifiable."""


class AuthConfigError(RuntimeError):
    """Auth is required but not usably configured. Raised at startup, not per request."""


class VerifiedIdentity(BaseModel):
    """The authenticated subject. ``roles`` are claim-supplied hints only —
    the authoritative role assignment still comes from ``principal_roles`` via
    ``RbacEngine``, so an identity provider cannot grant itself capabilities in
    Chandra by adding a claim."""

    principal_id: str
    tenant_id: str = DEFAULT_TENANT
    claimed_roles: list[str] = []
    raw_claims: dict[str, Any] = {}


def auth_enabled() -> bool:
    return bool(settings.auth_required)


def _public_key() -> str:
    key = settings.jwt_public_key
    if key and "\\n" in key:
        # Convenience: allow the PEM to be supplied with literal \n escapes, which
        # is how it survives most secret managers and env files. Checking for the
        # escape itself matters — an escaped PEM still contains "BEGIN", so
        # testing for that marker would skip the unescape and fail to parse.
        key = key.replace("\\n", "\n")
    if not key:
        raise AuthConfigError(
            "CHANDRA_AUTH_REQUIRED is set but JWT_PUBLIC_KEY is empty. "
            "Refusing to start with authentication silently disabled."
        )
    return key


def validate_auth_configuration() -> None:
    """Call once at startup. Fails loudly rather than degrading to no auth."""
    if auth_enabled():
        _public_key()
        logger.info(
            "auth.enabled",
            algorithm=ALGORITHM,
            issuer=settings.jwt_issuer or "(unverified)",
            audience=settings.jwt_audience or "(unverified)",
        )
    else:
        logger.warning(
            "auth.disabled",
            detail="CHANDRA_AUTH_REQUIRED is not set; principal is caller-asserted",
        )


def _first_claim(claims: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _roles_from_claims(claims: dict[str, Any]) -> list[str]:
    for name in _ROLE_CLAIMS:
        value = claims.get(name)
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str) and value.strip():
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


def decode_token(token: str, public_key: str | None = None) -> VerifiedIdentity:
    """Verify a bearer token and extract the identity. Raises ``AuthError``."""
    key = public_key if public_key is not None else _public_key()
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],  # pinned: never trust the token's own alg header
            audience=settings.jwt_audience or None,
            issuer=settings.jwt_issuer or None,
            options={
                "require": ["exp"],
                "verify_exp": True,
                "verify_aud": bool(settings.jwt_audience),
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signature, wrong algorithm, wrong issuer/audience, missing
        # exp, malformed token. The reason is logged, not returned: telling an
        # unauthenticated caller precisely why verification failed is free
        # reconnaissance.
        logger.warning("auth.token_rejected", reason=str(exc))
        raise AuthError("Token is not valid") from exc

    principal_id = _first_claim(claims, _PRINCIPAL_CLAIMS)
    if not principal_id:
        raise AuthError("Token carries no usable principal claim")

    return VerifiedIdentity(
        principal_id=principal_id,
        tenant_id=_first_claim(claims, _TENANT_CLAIMS) or DEFAULT_TENANT,
        claimed_roles=_roles_from_claims(claims),
        raw_claims=claims,
    )


def bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()

"""Resolution memory — the Proposedflow 'Past History Database'.

Before asking Bedrock for a plan, the worker checks whether an
equivalent request was solved before. Fingerprints are a stable hash of
the classified request (category + platform + services + salient title
tokens), so cosmetic differences in phrasing still hit the cache.

Write discipline: this module only *reads* Postgres. Persisting a new
plan (or bumping hit counts) happens exclusively inside the workflow
graph's ``persist`` node via :func:`persist_plan`, which is invoked with
an open session owned by that node.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from src.chandra.config import settings
from src.chandra.db.models import ResolutionMemoryRecord
from src.chandra.db.session import session_scope
from src.chandra.digital_worker.schemas import (
    CloudRequest,
    RequestClassification,
    ResolutionPlan,
)
from src.chandra.logging import get_logger
from src.chandra.memory import SemanticMemoryIndex

logger = get_logger(__name__)

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "in",
        "on",
        "for",
        "to",
        "of",
        "and",
        "or",
        "is",
        "are",
        "with",
        "please",
        "need",
        "we",
        "our",
        "my",
        "this",
        "that",
    }
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-]*")


def fingerprint_request(request: CloudRequest, classification: RequestClassification) -> str:
    """Stable SHA-256 fingerprint of the *classified* request."""
    tokens = sorted(
        {
            token
            for token in _TOKEN.findall(request.title.lower())
            if token not in _STOPWORDS and len(token) > 2
        }
    )
    basis = "|".join(
        [
            classification.category.value,
            classification.platform.value,
            ",".join(classification.services),
            ",".join(tokens[:12]),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def lookup_plan(fingerprint: str) -> ResolutionPlan | None:
    """Return the cached plan for ``fingerprint``, or ``None``.

    Degrades to a cache miss (with a structured warning) when Postgres is
    unreachable — memory is an optimization, never a hard dependency.
    """
    try:
        with session_scope() as session:
            record = session.execute(
                select(ResolutionMemoryRecord).where(
                    ResolutionMemoryRecord.fingerprint == fingerprint
                )
            ).scalar_one_or_none()
            if record is None:
                return None
            plan = ResolutionPlan.model_validate(record.plan_jsonb)
            plan.generated_by = "memory"
            plan.fingerprint = fingerprint
            logger.info(
                "memory.cache_hit",
                fingerprint=fingerprint,
                hit_count=record.hit_count,
                last_outcome=record.last_outcome,
            )
            return plan
    except Exception as exc:  # DB down → treat as miss, workflow continues
        logger.warning("memory.lookup_unavailable", fingerprint=fingerprint, error=str(exc))
        return None


def _memory_text(request: CloudRequest, classification: RequestClassification) -> str:
    """The text a semantic lookup matches on: what the request is about, in the
    classified form, so phrasing varies but meaning does not."""
    services = " ".join(getattr(classification, "services", []) or [])
    return " ".join(
        part
        for part in (
            request.title,
            (request.description or "")[:400],
            classification.category.value,
            classification.platform.value,
            services,
        )
        if part
    )


def lookup_plan_semantic(
    request: CloudRequest,
    classification: RequestClassification,
    index: SemanticMemoryIndex | None = None,
) -> ResolutionPlan | None:
    """Second memory tier: a prior request that *means* the same thing (§26.6).

    Runs only after an exact fingerprint miss. The returned plan is marked
    ``generated_by="memory_semantic"`` so its provenance is visible in the audit
    trail and distinguishable from an exact hit — a reviewer should be able to
    tell that a plan was matched by similarity rather than identity.

    Like every memory tier this is an accelerator, never an authorisation: the
    plan still traverses policy, risk, Gate 1 and Gate 2.
    """
    if not settings.semantic_memory_enabled:
        return None
    try:
        index = index if index is not None else build_semantic_index()
        if len(index) == 0:
            return None
        hit = index.best_match(_memory_text(request, classification))
        if hit is None:
            return None
        with session_scope() as session:
            record = session.execute(
                select(ResolutionMemoryRecord).where(
                    ResolutionMemoryRecord.fingerprint == hit.fingerprint
                )
            ).scalar_one_or_none()
            if record is None:
                return None
            plan = ResolutionPlan.model_validate(record.plan_jsonb)
            plan.generated_by = "memory_semantic"
            plan.fingerprint = hit.fingerprint
        logger.info(
            "memory.semantic_hit",
            fingerprint=hit.fingerprint,
            score=round(hit.score, 4),
            backend=index.backend,
        )
        return plan
    except Exception as exc:  # any failure is a cache miss, never a request failure
        logger.warning("memory.semantic_unavailable", error=str(exc))
        return None


def build_semantic_index(limit: int = 2000) -> SemanticMemoryIndex:
    """Build the vector index from resolution memory.

    Rebuilt rather than persisted: a stale index that disagrees with Postgres is
    a worse failure than recomputing cheap vectors.
    """
    index = SemanticMemoryIndex()
    with session_scope() as session:
        rows = session.scalars(
            select(ResolutionMemoryRecord)
            .order_by(ResolutionMemoryRecord.hit_count.desc())
            .limit(limit)
        ).all()
        for row in rows:
            index.add(
                row.fingerprint,
                " ".join(str(p) for p in (row.title, row.category, row.platform) if p),
                title=row.title,
            )
    index.build()
    return index


def persist_plan(
    session: Session,
    request: CloudRequest,
    classification: RequestClassification,
    plan: ResolutionPlan,
    outcome: str,
) -> None:
    """Upsert the plan into resolution_memory.

    MUST only be called from the workflow graph's ``persist`` node — the
    caller owns the transaction.
    """
    record = session.execute(
        select(ResolutionMemoryRecord).where(ResolutionMemoryRecord.fingerprint == plan.fingerprint)
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None:
        session.add(
            ResolutionMemoryRecord(
                fingerprint=plan.fingerprint,
                category=classification.category.value,
                platform=classification.platform.value,
                title=request.title,
                plan_jsonb=plan.model_dump(mode="json"),
                hit_count=1 if plan.generated_by == "memory" else 0,
                last_outcome=outcome,
                updated_at=now,
            )
        )
    else:
        if plan.generated_by == "memory":
            record.hit_count += 1
        else:
            record.plan_jsonb = plan.model_dump(mode="json")
        record.last_outcome = outcome
        record.updated_at = now

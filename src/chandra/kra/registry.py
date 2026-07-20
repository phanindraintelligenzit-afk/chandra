"""Dynamic KRA registry: "add a KRA = add a row, not a release".

The five built-in KRAs live here as code-level defaults so Chandra always
works with no database. Customer-defined KRAs (Sustainability, FinOps,
AI Governance, Business Continuity, …) are rows in ``kra_definitions``;
they participate in classification, prompting, and notification routing
the moment they are inserted — no source change, no redeploy.

Division of labour (unchanged invariants):

* The classifier/LLM only *identifies* the KRA for a request.
* The registry describes *how* that KRA is handled (mode, prompt
  template, rules); deterministic detectors remain the only execution
  path for ``deterministic``/``hybrid`` KRAs.
* Database reads degrade gracefully: with Postgres down, the built-ins
  (plus nothing else) are served and a warning is logged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from src.chandra.db.models import KraDefinitionRecord
from src.chandra.db.session import session_scope
from src.chandra.logging import get_logger

logger = get_logger(__name__)

KraMode = Literal["deterministic", "llm", "hybrid"]


class KraDefinition(BaseModel):
    """One KRA as seen by the rest of the platform."""

    code: str = Field(description="Stable machine id, e.g. 'cost' or 'finops'.")
    name: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    prompt_template: str = ""
    mode: KraMode = "llm"
    rules: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    builtin: bool = False


#: The governed core five. ``deterministic`` — they are backed by the boto3
#: detector suites in ``src/chandra/tools`` and the core LangGraph fan-out.
BUILTIN_KRAS: tuple[KraDefinition, ...] = (
    KraDefinition(
        code="cost",
        name="Cost",
        description="Cloud cost anomaly detection & optimization.",
        keywords=["cost", "spend", "billing", "budget", "idle", "unused", "savings"],
        mode="deterministic",
        builtin=True,
    ),
    KraDefinition(
        code="security",
        name="Security",
        description="Security posture, IAM drift, exposure detection.",
        keywords=["security", "public", "exposed", "iam", "breach", "vulnerability"],
        mode="deterministic",
        builtin=True,
    ),
    KraDefinition(
        code="compliance",
        name="Compliance",
        description="Compliance evidence and configuration conformance.",
        keywords=["compliance", "encryption", "audit", "config", "policy", "tagging"],
        mode="deterministic",
        builtin=True,
    ),
    KraDefinition(
        code="performance",
        name="Performance",
        description="Latency, throughput, saturation, right-sizing.",
        keywords=["performance", "latency", "slow", "throughput", "cpu", "saturation"],
        mode="deterministic",
        builtin=True,
    ),
    KraDefinition(
        code="reliability",
        name="Reliability",
        description="Uptime, failover, redundancy, backup coverage.",
        keywords=["reliability", "uptime", "failover", "backup", "redundancy", "outage"],
        mode="deterministic",
        builtin=True,
    ),
)


def _row_to_definition(row: KraDefinitionRecord) -> KraDefinition:
    return KraDefinition(
        code=row.code,
        name=row.name,
        description=row.description or "",
        keywords=[str(k) for k in (row.keywords_jsonb or [])],
        prompt_template=row.prompt_template or "",
        mode=row.mode if row.mode in ("deterministic", "llm", "hybrid") else "llm",
        rules=dict(row.rules_jsonb or {}),
        enabled=bool(row.enabled),
        builtin=False,
    )


def list_kras(include_disabled: bool = False) -> list[KraDefinition]:
    """Built-ins merged with database rows (DB overrides built-in by code)."""
    merged: dict[str, KraDefinition] = {k.code: k for k in BUILTIN_KRAS}
    try:
        with session_scope() as session:
            rows = session.execute(select(KraDefinitionRecord)).scalars().all()
            for row in rows:
                definition = _row_to_definition(row)
                if definition.builtin is False and merged.get(definition.code, definition).builtin:
                    # A DB row overriding a built-in keeps deterministic mode
                    # unless it explicitly says otherwise.
                    definition = definition.model_copy(update={"builtin": True})
                merged[definition.code] = definition
    except SQLAlchemyError as exc:
        logger.warning("kra_registry.db_unavailable_serving_builtins", error=str(exc))
    kras = list(merged.values())
    if not include_disabled:
        kras = [k for k in kras if k.enabled]
    return kras


def upsert_kra(definition: KraDefinition) -> KraDefinition:
    """Create or update one KRA row. Raises on database failure —
    a registry write the caller thinks succeeded must never be lost."""
    with session_scope() as session:
        row = (
            session.execute(
                select(KraDefinitionRecord).where(KraDefinitionRecord.code == definition.code)
            )
            .scalars()
            .first()
        )
        if row is None:
            row = KraDefinitionRecord(code=definition.code, name=definition.name)
            session.add(row)
        row.name = definition.name
        row.description = definition.description
        row.keywords_jsonb = list(definition.keywords)
        row.prompt_template = definition.prompt_template
        row.mode = definition.mode
        row.rules_jsonb = dict(definition.rules)
        row.enabled = definition.enabled
    logger.info("kra_registry.upserted", code=definition.code, mode=definition.mode)
    return definition


def match_kra(text: str) -> KraDefinition | None:
    """Best keyword match for free-form request text, or ``None``.

    Deterministic scoring (hit count, ties broken by declaration order) so
    the same request always maps to the same KRA. The LLM path may refine
    this, but never bypasses the registry.
    """
    tokens = set(text.lower().split())
    best: KraDefinition | None = None
    best_hits = 0
    for kra in list_kras():
        hits = sum(1 for kw in kra.keywords if kw.lower() in tokens)
        if hits > best_hits:
            best, best_hits = kra, hits
    return best

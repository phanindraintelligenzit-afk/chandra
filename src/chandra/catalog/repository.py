"""Postgres-backed store for Digital Worker configuration (PRD §26.8).

Replaces the JSON files that used to live at the repo root:

    aws_tasks.json        -> aws_tasks
    aws_permissions.json  -> permission_sets
    customKras.json       -> custom_kras
    agent_memory.json     -> agent_run_memory

This module is the **only** place these tables are written. Every function is
tenant-scoped. The wire format returned to the API is kept identical to the
old JSON shapes so the Next.js console needs no change.

Rows are stored as-is (no LLM involvement); ``agent_run_memory`` is knowledge
only and is never read by any authorization check (PRD §26.6).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from src.chandra.db.models import (
    AgentRunMemoryRecord,
    AwsTaskRecord,
    CustomKraRecord,
    PermissionSetRecord,
)
from src.chandra.db.session import session_scope as _default_session_scope
from src.chandra.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TENANT = "default"

SessionFactory = Callable[[], AbstractContextManager[Session]]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Normalisation (moved verbatim in behaviour from fastapi_app.py)
# ---------------------------------------------------------------------------


def normalize_custom_kras(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Accept strings or dicts; dedupe by case-insensitive name; fill description."""
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
            description = name
            selected = True
        elif isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("code") or "").strip()
            description = str(entry.get("description") or entry.get("desc") or name).strip()
            selected = bool(entry.get("selected", True))
        else:
            continue
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({"name": name, "description": description or name, "selected": selected})
    return cleaned


_TASK_FIELDS = {
    "id",
    "name",
    "description",
    "category",
    "ownership",
    "version",
    "is_preset",
    "is_predefined",
}
_PERM_FIELDS = {
    "id",
    "name",
    "description",
    "aws_service",
    "actions",
    "resource_arn",
    "is_predefined",
    "created_by",
    "version",
    "created_at",
    "updated_at",
}


def _task_to_row(entry: dict[str, Any], tenant_id: str) -> AwsTaskRecord | None:
    task_id = str(entry.get("id") or "").strip()
    name = str(entry.get("name") or "").strip()
    if not task_id or not name:
        return None
    return AwsTaskRecord(
        id=task_id,
        tenant_id=tenant_id,
        name=name,
        description=str(entry.get("description") or ""),
        category=str(entry.get("category") or ""),
        ownership=str(entry.get("ownership") or ""),
        version=int(entry.get("version") or 1),
        is_preset=bool(entry.get("is_preset", False)),
        is_predefined=bool(entry.get("is_predefined", False)),
        extra_jsonb={k: v for k, v in entry.items() if k not in _TASK_FIELDS},
        updated_at=_now(),
    )


def _task_to_dict(row: AwsTaskRecord) -> dict[str, Any]:
    out: dict[str, Any] = dict(row.extra_jsonb or {})
    out.update(
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "category": row.category,
            "ownership": row.ownership,
            "version": row.version,
            "is_preset": row.is_preset,
            "is_predefined": row.is_predefined,
        }
    )
    return out


def _perm_to_row(entry: dict[str, Any], tenant_id: str) -> PermissionSetRecord | None:
    pid = str(entry.get("id") or "").strip()
    name = str(entry.get("name") or "").strip()
    if not pid or not name:
        return None
    actions = entry.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    return PermissionSetRecord(
        id=pid,
        tenant_id=tenant_id,
        name=name,
        description=str(entry.get("description") or ""),
        aws_service=str(entry.get("aws_service") or ""),
        actions_jsonb=[str(a) for a in actions],
        resource_arn=str(entry.get("resource_arn") or "*"),
        is_predefined=bool(entry.get("is_predefined", False)),
        created_by=str(entry.get("created_by") or ""),
        version=int(entry.get("version") or 1),
        extra_jsonb={k: v for k, v in entry.items() if k not in _PERM_FIELDS},
        created_at=_parse_ts(entry.get("created_at")) or _now(),
        updated_at=_parse_ts(entry.get("updated_at")) or _now(),
    )


def _perm_to_dict(row: PermissionSetRecord) -> dict[str, Any]:
    out: dict[str, Any] = dict(row.extra_jsonb or {})
    out.update(
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "aws_service": row.aws_service,
            "actions": list(row.actions_jsonb or []),
            "resource_arn": row.resource_arn,
            "is_predefined": row.is_predefined,
            "created_by": row.created_by,
            "version": row.version,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }
    )
    return out


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ConfigRepository:
    """Tenant-scoped CRUD over the four configuration tables.

    ``session_factory`` is injectable so tests run against SQLite without a
    Postgres instance; production uses ``chandra.db.session.session_scope``.
    """

    def __init__(
        self,
        tenant_id: str = DEFAULT_TENANT,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self.tenant_id = tenant_id or DEFAULT_TENANT
        self._scope: SessionFactory = session_factory or _default_session_scope

    # -- AWS tasks --------------------------------------------------------

    def list_aws_tasks(self) -> list[dict[str, Any]]:
        with self._scope() as s:
            rows = s.scalars(
                select(AwsTaskRecord)
                .where(AwsTaskRecord.tenant_id == self.tenant_id)
                .order_by(AwsTaskRecord.created_at, AwsTaskRecord.id)
            ).all()
            return [_task_to_dict(r) for r in rows]

    def replace_aws_tasks(self, entries: Iterable[dict[str, Any]]) -> int:
        """Full replace (PUT semantics, matches the old file overwrite)."""
        rows = [r for r in (_task_to_row(e, self.tenant_id) for e in entries) if r is not None]
        with self._scope() as s:
            s.execute(delete(AwsTaskRecord).where(AwsTaskRecord.tenant_id == self.tenant_id))
            s.add_all(rows)
        logger.info("catalog.aws_tasks.replaced", tenant=self.tenant_id, count=len(rows))
        return len(rows)

    # -- Permission sets --------------------------------------------------

    def list_permission_sets(self) -> list[dict[str, Any]]:
        with self._scope() as s:
            rows = s.scalars(
                select(PermissionSetRecord)
                .where(PermissionSetRecord.tenant_id == self.tenant_id)
                .order_by(PermissionSetRecord.created_at, PermissionSetRecord.id)
            ).all()
            return [_perm_to_dict(r) for r in rows]

    def get_permission_set(self, permission_set_id: str) -> dict[str, Any] | None:
        with self._scope() as s:
            row = s.get(PermissionSetRecord, (permission_set_id, self.tenant_id))
            return _perm_to_dict(row) if row else None

    def replace_permission_sets(self, entries: Iterable[dict[str, Any]]) -> int:
        rows = [r for r in (_perm_to_row(e, self.tenant_id) for e in entries) if r is not None]
        with self._scope() as s:
            s.execute(
                delete(PermissionSetRecord).where(PermissionSetRecord.tenant_id == self.tenant_id)
            )
            s.add_all(rows)
        logger.info("catalog.permission_sets.replaced", tenant=self.tenant_id, count=len(rows))
        return len(rows)

    # -- Custom KRAs ------------------------------------------------------

    def list_custom_kras(self) -> list[dict[str, Any]]:
        with self._scope() as s:
            rows = s.scalars(
                select(CustomKraRecord)
                .where(CustomKraRecord.tenant_id == self.tenant_id)
                .order_by(CustomKraRecord.position, CustomKraRecord.name)
            ).all()
            return [
                {"name": r.name, "description": r.description, "selected": r.selected} for r in rows
            ]

    def replace_custom_kras(self, entries: Iterable[Any]) -> int:
        cleaned = normalize_custom_kras(entries)
        with self._scope() as s:
            s.execute(delete(CustomKraRecord).where(CustomKraRecord.tenant_id == self.tenant_id))
            s.add_all(
                CustomKraRecord(
                    tenant_id=self.tenant_id,
                    name=e["name"],
                    description=e["description"],
                    selected=e["selected"],
                    position=i,
                )
                for i, e in enumerate(cleaned)
            )
        return len(cleaned)

    # -- Agent run memory -------------------------------------------------

    def record_agent_run(
        self,
        *,
        action_name: str,
        final_status: str,
        iterations_used: int,
        errors: list[Any],
        fixes: list[Any],
        lesson: str,
        correlation_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        with self._scope() as s:
            s.add(
                AgentRunMemoryRecord(
                    tenant_id=self.tenant_id,
                    correlation_id=correlation_id,
                    action_name=action_name,
                    final_status=final_status,
                    iterations_used=iterations_used,
                    errors_jsonb=list(errors),
                    fixes_jsonb=list(fixes),
                    lesson=lesson,
                    recorded_at=recorded_at or _now(),
                )
            )

    def recent_agent_runs(
        self, *, action_name: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        stmt = select(AgentRunMemoryRecord).where(AgentRunMemoryRecord.tenant_id == self.tenant_id)
        if action_name:
            stmt = stmt.where(AgentRunMemoryRecord.action_name == action_name)
        stmt = stmt.order_by(AgentRunMemoryRecord.recorded_at.desc()).limit(limit)
        with self._scope() as s:
            rows = s.scalars(stmt).all()
            return [
                {
                    "timestamp": _iso(r.recorded_at),
                    "action_name": r.action_name,
                    "iterations_used": r.iterations_used,
                    "final_status": r.final_status,
                    "errors_encountered": list(r.errors_jsonb or []),
                    "fixes_applied": list(r.fixes_jsonb or []),
                    "lesson": r.lesson,
                    "correlation_id": r.correlation_id,
                }
                for r in rows
            ]

    # -- Bulk import (one-off migration from the legacy JSON files) -------

    def import_legacy_json(
        self,
        *,
        aws_tasks: list[dict[str, Any]] | None = None,
        permission_sets: list[dict[str, Any]] | None = None,
        custom_kras: list[Any] | None = None,
        agent_memory: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> dict[str, int]:
        """Load the legacy JSON payloads. Skips a table that already has rows
        unless ``overwrite`` is set, so re-running is safe."""
        result: dict[str, int] = {}
        if aws_tasks is not None and (overwrite or not self.list_aws_tasks()):
            result["aws_tasks"] = self.replace_aws_tasks(aws_tasks)
        if permission_sets is not None and (overwrite or not self.list_permission_sets()):
            result["permission_sets"] = self.replace_permission_sets(permission_sets)
        if custom_kras is not None and (overwrite or not self.list_custom_kras()):
            result["custom_kras"] = self.replace_custom_kras(custom_kras)
        if agent_memory is not None and (overwrite or not self.recent_agent_runs(limit=1)):
            runs = agent_memory.get("runs") or []
            for run in runs:
                if not isinstance(run, dict):
                    continue
                self.record_agent_run(
                    action_name=str(run.get("action_name") or "unknown"),
                    final_status=str(run.get("final_status") or "unknown"),
                    iterations_used=int(run.get("iterations_used") or 0),
                    errors=list(run.get("errors_encountered") or []),
                    fixes=list(run.get("fixes_applied") or []),
                    lesson=str(run.get("lesson") or ""),
                    recorded_at=_parse_ts(run.get("timestamp")),
                )
            result["agent_run_memory"] = len(runs)
        logger.info("catalog.legacy_import", tenant=self.tenant_id, **result)
        return result

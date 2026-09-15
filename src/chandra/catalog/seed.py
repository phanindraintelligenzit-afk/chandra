"""Seed / import the Digital Worker configuration tables from JSON files.

Two layouts are accepted:

* the packaged seeds in ``src/chandra/catalog/seeds/`` (fresh install), and
* the legacy repo-root layout (``aws_permissions.json``, ``customKras.json`` …)
  for one-off migration of an existing deployment.

Used by ``chandra catalog seed``. Idempotent unless ``overwrite`` is passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.chandra.catalog.repository import DEFAULT_TENANT, ConfigRepository
from src.chandra.logging import get_logger

logger = get_logger(__name__)

SEEDS_DIR = Path(__file__).parent / "seeds"

# canonical name -> accepted file names (first match wins)
_FILES: dict[str, tuple[str, ...]] = {
    "aws_tasks": ("aws_tasks.json",),
    "permission_sets": ("permission_sets.json", "aws_permissions.json"),
    "custom_kras": ("custom_kras.json", "customKras.json"),
    "agent_memory": ("agent_memory.json",),
    "digital_worker_config": ("digital_worker_config.json",),
}


def _read(directory: Path, names: tuple[str, ...]) -> Any:
    for name in names:
        path = directory / name
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    return None


def load_seed_dir(directory: Path) -> dict[str, Any]:
    """Return the payloads found in ``directory`` keyed by canonical name."""
    found = {key: _read(directory, names) for key, names in _FILES.items()}
    return {k: v for k, v in found.items() if v is not None}


def seed_catalog(
    directory: Path | None = None,
    *,
    tenant_id: str = DEFAULT_TENANT,
    overwrite: bool = False,
    repo: ConfigRepository | None = None,
) -> dict[str, int]:
    """Load the JSON files in ``directory`` (default: packaged seeds) into Postgres."""
    directory = directory or SEEDS_DIR
    payloads = load_seed_dir(directory)
    if not payloads:
        logger.warning("catalog.seed.nothing_found", directory=str(directory))
        return {}
    repo = repo or ConfigRepository(tenant_id=tenant_id)
    return repo.import_legacy_json(overwrite=overwrite, **payloads)

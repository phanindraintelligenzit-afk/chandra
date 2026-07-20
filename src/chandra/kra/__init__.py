"""Dynamic KRA Framework — registry package.

A KRA is a row, not code. See :mod:`src.chandra.kra.registry`.
"""

from src.chandra.kra.registry import (
    BUILTIN_KRAS,
    KraDefinition,
    list_kras,
    match_kra,
    upsert_kra,
)

__all__ = ["BUILTIN_KRAS", "KraDefinition", "list_kras", "match_kra", "upsert_kra"]

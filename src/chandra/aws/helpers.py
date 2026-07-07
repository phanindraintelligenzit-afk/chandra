"""Small AWS helpers shared by graph nodes.

``get_factory_for_state`` resolves the client factory a node should use:
the default factory, or a cross-account assumed-role factory when the
graph state carries an ``assume_role_arn``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.chandra.aws.client_factory import AwsClientFactory, get_default_factory


def get_factory_for_state(state: Mapping[str, Any]) -> AwsClientFactory:
    """Return the :class:`AwsClientFactory` appropriate for ``state``.

    When ``state["assume_role_arn"]`` is set, returns a factory whose
    session runs under that assumed role; otherwise the process default.
    """
    factory = get_default_factory()
    role_arn = state.get("assume_role_arn")
    if role_arn:
        return factory.assume_role(
            role_arn=role_arn,
            session_name="chandra-cross-account",
        )
    return factory

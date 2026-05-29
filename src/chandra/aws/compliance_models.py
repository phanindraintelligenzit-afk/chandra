"""Compliance finding dataclass for AWS Config violations."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ComplianceFinding:
    """Structured compliance finding from AWS Config."""

    kra: str
    resource_id: str
    rule_name: str
    status: str
    finding_details: str
    severity: str
    raw_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), default=str)

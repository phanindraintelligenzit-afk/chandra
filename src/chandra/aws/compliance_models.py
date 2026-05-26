from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ComplianceFinding:
    kra: str
    resource_id: str
    rule_name: str
    status: str
    finding_details: str
    severity: str
    raw_payload: dict[str, Any]

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        import json
        return json.dumps(self.to_dict(), default=str)

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class SecurityFinding:
    kra: str
    service: str
    resource_id: str
    finding_type: str
    severity: str
    raw_payload: dict[str, Any]

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        import json
        return json.dumps(self.to_dict())
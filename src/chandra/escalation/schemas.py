from pydantic import BaseModel
from typing import Literal


class EscalationPayload(BaseModel):
    finding_id: str
    resource_id: str
    severity: Literal["low", "medium", "high", "critical"]
    service: str
    region: str
    summary: str
    recommended_action: str


class EscalationResult(BaseModel):
    status: str
    message_id: str | None = None
    error: str | None = None
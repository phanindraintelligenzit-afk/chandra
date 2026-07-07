from src.chandra.escalation.schemas import EscalationPayload


def format_escalation_message(payload: EscalationPayload) -> dict[str, str]:
    return {
        "finding_id": payload.finding_id,
        "resource_id": payload.resource_id,
        "severity": payload.severity,
        "service": payload.service,
        "region": payload.region,
        "summary": payload.summary,
        "recommended_action": payload.recommended_action,
    }

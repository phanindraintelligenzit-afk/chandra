"""CloudTrail audit scanner."""

from __future__ import annotations

import boto3
from src.chandra.aws.compliance_models import ComplianceFinding


def scan_cloudtrail() -> list[ComplianceFinding]:
    """Scan CloudTrail for disabled logging."""
    
    client = boto3.client("cloudtrail")
    findings: list[ComplianceFinding] = []

    try:
        response = client.describe_trails()
        
        for trail in response.get("trailList", []):
            trail_name = trail.get("Name", "unknown")
            is_logging = trail.get("IsLogging", True)
            
            if not is_logging:
                findings.append(
                    ComplianceFinding(
                        kra="Compliance",
                        resource_id=trail_name,
                        rule_name="CloudTrailLogging",
                        status="NON_COMPLIANT",
                        finding_details=f"CloudTrail {trail_name} logging disabled",
                        severity="CRITICAL",
                        raw_payload=trail,
                    )
                )

    except Exception as e:
        print(f"Error scanning CloudTrail: {e}")

    return findings

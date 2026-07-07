"""AWS Config compliance scanner."""

from __future__ import annotations

import boto3
from src.chandra.aws.compliance_models import ComplianceFinding


def scan_config_compliance() -> list[ComplianceFinding]:
    """Scan AWS Config for non-compliant rules."""

    client = boto3.client("config")
    findings: list[ComplianceFinding] = []

    try:
        response = client.describe_compliance_by_config_rule()

        for rule in response.get("ComplianceByConfigRules", []):
            rule_name = rule.get("ConfigRuleName", "unknown")
            compliance_type = rule.get("Compliance", {}).get("ComplianceType", "UNKNOWN")

            if compliance_type == "NON_COMPLIANT":
                findings.append(
                    ComplianceFinding(
                        kra="Compliance",
                        resource_id=rule_name,
                        rule_name=rule_name,
                        status="NON_COMPLIANT",
                        finding_details=f"AWS Config rule {rule_name} is non-compliant",
                        severity="HIGH",
                        raw_payload=rule,
                    )
                )

    except Exception as e:
        print(f"Error scanning Config: {e}")

    return findings

import boto3
from chandra.aws.security_models import SecurityFinding


def scan_security_hub():
    findings = []
    sh = boto3.client("securityhub")

    try:
        paginator = sh.get_paginator("get_findings")
        pages = paginator.paginate()

        for page in pages:
            for finding in page.get("Findings", []):
                findings.append(
                    SecurityFinding(
                        kra="Security",
                        service="SecurityHub",
                        resource_id=finding["Id"],
                        finding_type=finding.get("Title", "Unknown"),
                        severity=finding.get("Severity", {}).get("Label", "UNKNOWN"),
                        raw_payload=finding,
                    )
                )
    except Exception as e:
        print(f"Error scanning Security Hub: {e}")

    return findings
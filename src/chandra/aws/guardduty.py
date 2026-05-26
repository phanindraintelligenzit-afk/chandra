import boto3
from chandra.aws.security_models import SecurityFinding


def scan_guardduty():
    findings = []
    gd = boto3.client("guardduty")

    try:
        detectors = gd.list_detectors()["DetectorIds"]
    except Exception as e:
        print(f"Error listing GuardDuty detectors: {e}")
        return findings

    for detector in detectors:
        try:
            response = gd.list_findings(DetectorId=detector)
            finding_ids = response.get("FindingIds", [])

            for finding_id in finding_ids:
                findings.append(
                    SecurityFinding(
                        kra="Security",
                        service="GuardDuty",
                        resource_id=finding_id,
                        finding_type="ThreatDetected",
                        severity="HIGH",
                        raw_payload={"finding_id": finding_id, "detector_id": detector},
                    )
                )
        except Exception as e:
            print(f"Error scanning detector {detector}: {e}")

    return findings
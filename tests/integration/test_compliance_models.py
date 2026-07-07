from src.chandra.aws.compliance_models import ComplianceFinding


def test_compliance_finding_creation():
    finding = ComplianceFinding(
        kra="Compliance",
        resource_id="arn:aws:s3:::test",
        rule_name="S3Encryption",
        status="NON_COMPLIANT",
        finding_details="Test finding",
        severity="HIGH",
        raw_payload={},
    )

    assert finding.kra == "Compliance"
    assert finding.status == "NON_COMPLIANT"


def test_compliance_finding_to_dict():
    finding = ComplianceFinding(
        kra="Compliance",
        resource_id="arn:aws:s3:::test",
        rule_name="S3Encryption",
        status="NON_COMPLIANT",
        finding_details="Test finding",
        severity="HIGH",
        raw_payload={},
    )

    d = finding.to_dict()
    assert isinstance(d, dict)
    assert d["kra"] == "Compliance"


def test_compliance_finding_json_serializable():
    import json

    finding = ComplianceFinding(
        kra="Compliance",
        resource_id="arn:aws:s3:::test",
        rule_name="S3Encryption",
        status="NON_COMPLIANT",
        finding_details="Test finding",
        severity="HIGH",
        raw_payload={},
    )

    json_str = finding.to_json()
    assert isinstance(json_str, str)
    parsed = json.loads(json_str)
    assert parsed["kra"] == "Compliance"

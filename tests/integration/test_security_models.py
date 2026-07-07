from src.chandra.aws.security_models import SecurityFinding


def test_security_finding_creation():
    finding = SecurityFinding(
        kra="Security",
        service="S3",
        resource_id="arn:aws:s3:::test",
        finding_type="PublicAccess",
        severity="CRITICAL",
        raw_payload={},
    )

    assert finding.kra == "Security"
    assert finding.service == "S3"


def test_security_finding_to_dict():
    finding = SecurityFinding(
        kra="Security",
        service="S3",
        resource_id="arn:aws:s3:::test",
        finding_type="PublicAccess",
        severity="CRITICAL",
        raw_payload={},
    )

    d = finding.to_dict()
    assert isinstance(d, dict)
    assert d["kra"] == "Security"


def test_security_finding_json_serializable():
    import json

    finding = SecurityFinding(
        kra="Security",
        service="S3",
        resource_id="arn:aws:s3:::test",
        finding_type="PublicAccess",
        severity="CRITICAL",
        raw_payload={},
    )

    json_str = finding.to_json()
    assert isinstance(json_str, str)
    parsed = json.loads(json_str)
    assert parsed["kra"] == "Security"

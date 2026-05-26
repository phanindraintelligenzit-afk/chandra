import json
from unittest.mock import patch, MagicMock
from chandra.aws.compliance_models import ComplianceFinding


def test_compliance_finding_structure():
    finding = ComplianceFinding(
        kra="Compliance",
        resource_id="arn:aws:s3:::test-bucket",
        rule_name="S3Encryption",
        status="NON_COMPLIANT",
        finding_details="S3 bucket encryption disabled",
        severity="HIGH",
        raw_payload={"bucket": "test"}
    )
    
    finding_dict = finding.to_dict()
    assert finding_dict["kra"] == "Compliance"
    assert finding_dict["status"] == "NON_COMPLIANT"
    print("✓ ComplianceFinding structure test PASSED")
    return finding_dict


def test_config_compliance_with_mock():
    from chandra.aws.config_compliance import scan_config_compliance
    
    with patch('chandra.aws.config_compliance.boto3.client') as mock_boto3:
        mock_config = MagicMock()
        mock_boto3.return_value = mock_config
        
        mock_config.describe_compliance_by_config_rule.return_value = {
            "ComplianceByConfigRules": [
                {
                    "ConfigRuleName": "s3-bucket-encryption",
                    "Compliance": {"ComplianceType": "NON_COMPLIANT"}
                },
                {
                    "ConfigRuleName": "ec2-security-group-open",
                    "Compliance": {"ComplianceType": "NON_COMPLIANT"}
                }
            ]
        }
        
        findings = scan_config_compliance()
        assert len(findings) == 2
        print(f"✓ Config compliance mock test PASSED")
        print(f"  Found {len(findings)} non-compliant rules")
        for f in findings:
            print(f"  - {f.rule_name} ({f.severity})")
        
        return findings


def test_cloudtrail_with_mock():
    from chandra.aws.cloudtrail_audit import scan_cloudtrail
    
    with patch('chandra.aws.cloudtrail_audit.boto3.client') as mock_boto3:
        mock_cloudtrail = MagicMock()
        mock_boto3.return_value = mock_cloudtrail
        
        mock_cloudtrail.describe_trails.return_value = {
            "trailList": [
                {"Name": "prod-trail", "IsLogging": False}
            ]
        }
        
        findings = scan_cloudtrail()
        assert len(findings) == 1
        print(f"✓ CloudTrail mock test PASSED")
        print(f"  Found {len(findings)} issues")
        for f in findings:
            print(f"  - {f.finding_details} ({f.severity})")
        
        return findings


def test_encryption_with_mock():
    from chandra.aws.encryption_checks import scan_s3_encryption
    
    with patch('chandra.aws.encryption_checks.boto3.client') as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3
        
        mock_s3.list_buckets.return_value = {
            "Buckets": [
                {"Name": "encrypted-bucket"},
                {"Name": "unencrypted-bucket"}
            ]
        }
        
        def mock_get_encryption(Bucket):
            if Bucket == "encrypted-bucket":
                return {"ServerSideEncryptionConfiguration": {"Rules": []}}
            else:
                raise Exception("ServerSideEncryptionConfigurationNotFoundError")
        
        mock_s3.get_bucket_encryption.side_effect = mock_get_encryption
        findings = scan_s3_encryption()
        
        assert len(findings) == 1
        print(f"✓ S3 encryption mock test PASSED")
        print(f"  Found {len(findings)} unencrypted buckets")
        for f in findings:
            print(f"  - {f.resource_id}: {f.finding_details}")
        
        return findings


def demo_all_compliance_scanners():
    print("=" * 80)
    print("CHANDRA COMPLIANCE KRA SCANNER - MOCK TEST (No AWS credentials needed)")
    print("=" * 80)
    
    all_findings = []
    
    try:
        print("\n[1] ComplianceFinding structure test...")
        f1 = test_compliance_finding_structure()
        all_findings.append(f1)
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    try:
        print("\n[2] Config Compliance Scanner (mocked)...")
        findings = test_config_compliance_with_mock()
        all_findings.extend([f.to_dict() for f in findings])
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    try:
        print("\n[3] CloudTrail Audit Scanner (mocked)...")
        findings = test_cloudtrail_with_mock()
        all_findings.extend([f.to_dict() for f in findings])
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    try:
        print("\n[4] S3 Encryption Check (mocked)...")
        findings = test_encryption_with_mock()
        all_findings.extend([f.to_dict() for f in findings])
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    print("\n" + "=" * 80)
    print(f"TOTAL COMPLIANCE FINDINGS: {len(all_findings)}")
    print("=" * 80)
    
    print("\nJSON OUTPUT (for LangGraph):")
    print(json.dumps(all_findings, indent=2, default=str))
    
    return all_findings


if __name__ == "__main__":
    demo_all_compliance_scanners()

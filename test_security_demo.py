import json
from unittest.mock import patch, MagicMock
from chandra.aws.security_models import SecurityFinding


def test_security_findings_structure():
    finding = SecurityFinding(
        kra="Security",
        service="S3",
        resource_id="arn:aws:s3:::test-bucket",
        finding_type="PublicAccessBlockDisabled",
        severity="CRITICAL",
        raw_payload={"test": "data"}
    )
    
    finding_dict = finding.to_dict()
    
    assert finding_dict["kra"] == "Security"
    assert finding_dict["service"] == "S3"
    assert finding_dict["resource_id"] == "arn:aws:s3:::test-bucket"
    assert finding_dict["finding_type"] == "PublicAccessBlockDisabled"
    assert finding_dict["severity"] == "CRITICAL"
    
    print("✓ SecurityFinding structure test PASSED")
    return finding_dict


def test_s3_scanner_with_mock():
    from chandra.aws.s3_public_access import scan_s3_public_access
    
    with patch('chandra.aws.s3_public_access.boto3.client') as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3
        
        mock_s3.list_buckets.return_value = {
            "Buckets": [
                {"Name": "secure-bucket"},
                {"Name": "insecure-bucket"}
            ]
        }
        
        def mock_get_public_access(Bucket):
            if Bucket == "secure-bucket":
                return {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True
                    }
                }
            else:
                return {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": False,
                        "IgnorePublicAcls": False,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True
                    }
                }
        
        mock_s3.get_public_access_block.side_effect = mock_get_public_access
        
        findings = scan_s3_public_access()
        
        assert len(findings) == 1
        assert findings[0].service == "S3"
        assert findings[0].finding_type == "PublicAccessBlockDisabled"
        assert findings[0].severity == "CRITICAL"
        
        print(f"✓ S3 scanner mock test PASSED")
        print(f"  Found {len(findings)} findings")
        for f in findings:
            print(f"  - {f.service}: {f.finding_type} ({f.severity})")
        
        return findings


def test_kms_scanner_with_mock():
    from chandra.aws.kms_scanner import scan_kms_rotation
    
    with patch('chandra.aws.kms_scanner.boto3.client') as mock_boto3:
        mock_kms = MagicMock()
        mock_boto3.return_value = mock_kms
        
        mock_kms.list_keys.return_value = {
            "Keys": [
                {"KeyId": "arn:aws:kms:us-east-1:123456789012:key/key-1"},
                {"KeyId": "arn:aws:kms:us-east-1:123456789012:key/key-2"}
            ]
        }
        
        def mock_get_rotation(KeyId):
            if "key-1" in KeyId:
                return {"KeyRotationEnabled": True}
            else:
                return {"KeyRotationEnabled": False}
        
        mock_kms.get_key_rotation_status.side_effect = mock_get_rotation
        
        findings = scan_kms_rotation()
        
        assert len(findings) == 1
        assert findings[0].service == "KMS"
        assert findings[0].finding_type == "KeyRotationDisabled"
        
        print(f"✓ KMS scanner mock test PASSED")
        print(f"  Found {len(findings)} findings")
        for f in findings:
            print(f"  - {f.service}: {f.finding_type} ({f.severity})")
        
        return findings


def demo_mock_findings():
    print("=" * 80)
    print("CHANDRA SECURITY KRA SCANNER - MOCK TEST (No AWS credentials needed)")
    print("=" * 80)
    
    all_findings = []
    
    try:
        print("\n[1] SecurityFinding structure test...")
        f1 = test_security_findings_structure()
        all_findings.append(f1)
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    try:
        print("\n[2] S3 Public Access Scanner (mocked)...")
        findings = test_s3_scanner_with_mock()
        all_findings.extend([f.to_dict() for f in findings])
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    try:
        print("\n[3] KMS Rotation Scanner (mocked)...")
        findings = test_kms_scanner_with_mock()
        all_findings.extend([f.to_dict() for f in findings])
    except Exception as e:
        print(f"  ✗ Error: {e}")
    
    print("\n" + "=" * 80)
    print(f"TOTAL FINDINGS: {len(all_findings)}")
    print("=" * 80)
    
    print("\nJSON OUTPUT (for LangGraph):")
    print(json.dumps(all_findings, indent=2, default=str))
    
    return all_findings


if __name__ == "__main__":
    demo_mock_findings()

import pytest
from unittest.mock import patch, MagicMock
from src.chandra.aws.encryption_checks import scan_s3_encryption, scan_ebs_encryption


def test_s3_encryption_returns_list():
    with patch('chandra.aws.encryption_checks.boto3.client') as mock_boto3:
        mock_s3 = MagicMock()
        mock_boto3.return_value = mock_s3
        mock_s3.list_buckets.return_value = {"Buckets": []}
        
        findings = scan_s3_encryption()
        assert isinstance(findings, list)


def test_ebs_encryption_detects_disabled():
    with patch('chandra.aws.encryption_checks.boto3.client') as mock_boto3:
        mock_ec2 = MagicMock()
        mock_boto3.return_value = mock_ec2
        mock_ec2.get_ebs_encryption_by_default.return_value = {
            "EbsEncryptionByDefault": False
        }
        
        findings = scan_ebs_encryption()
        assert len(findings) == 1
        assert findings[0].rule_name == "EBSEncryption"

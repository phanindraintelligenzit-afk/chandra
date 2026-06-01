import pytest
from unittest.mock import patch, MagicMock
from src.chandra.aws.cloudtrail_audit import scan_cloudtrail


def test_cloudtrail_returns_list():
    with patch('chandra.aws.cloudtrail_audit.boto3.client') as mock_boto3:
        mock_cloudtrail = MagicMock()
        mock_boto3.return_value = mock_cloudtrail
        mock_cloudtrail.describe_trails.return_value = {"trailList": []}
        
        findings = scan_cloudtrail()
        assert isinstance(findings, list)


def test_cloudtrail_detects_disabled_logging():
    with patch('chandra.aws.cloudtrail_audit.boto3.client') as mock_boto3:
        mock_cloudtrail = MagicMock()
        mock_boto3.return_value = mock_cloudtrail
        mock_cloudtrail.describe_trails.return_value = {
            "trailList": [
                {"Name": "test-trail", "IsLogging": False}
            ]
        }
        
        findings = scan_cloudtrail()
        assert len(findings) == 1
        assert findings[0].severity == "CRITICAL"

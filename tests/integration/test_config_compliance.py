import pytest
from unittest.mock import patch, MagicMock
from chandra.aws.config_compliance import scan_config_compliance


def test_config_compliance_returns_list():
    with patch('chandra.aws.config_compliance.boto3.client') as mock_boto3:
        mock_config = MagicMock()
        mock_boto3.return_value = mock_config
        mock_config.describe_compliance_by_config_rule.return_value = {
            "ComplianceByConfigRules": []
        }
        
        findings = scan_config_compliance()
        assert isinstance(findings, list)


def test_config_compliance_detects_non_compliant():
    with patch('chandra.aws.config_compliance.boto3.client') as mock_boto3:
        mock_config = MagicMock()
        mock_boto3.return_value = mock_config
        mock_config.describe_compliance_by_config_rule.return_value = {
            "ComplianceByConfigRules": [
                {
                    "ConfigRuleName": "s3-encryption",
                    "Compliance": {"ComplianceType": "NON_COMPLIANT"}
                }
            ]
        }
        
        findings = scan_config_compliance()
        assert len(findings) == 1
        assert findings[0].status == "NON_COMPLIANT"
        assert findings[0].kra == "Compliance"

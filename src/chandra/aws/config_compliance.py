import boto3
from chandra.aws.compliance_models import ComplianceFinding


def scan_config_compliance():
    findings = []
    config = boto3.client('config')

    try:
        response = config.describe_compliance_by_config_rule()
        
        for item in response.get('ComplianceByConfigRules', []):
            compliance = item.get('Compliance', {})
            
            if compliance.get('ComplianceType') == 'NON_COMPLIANT':
                findings.append(
                    ComplianceFinding(
                        kra='Compliance',
                        resource_id=item.get('ConfigRuleName', 'unknown'),
                        rule_name=item.get('ConfigRuleName', 'unknown'),
                        status='NON_COMPLIANT',
                        finding_details='AWS Config rule violation detected',
                        severity='HIGH',
                        raw_payload=item,
                    )
                )
    except Exception as e:
        print(f'Error scanning Config rules: {e}')

    return findings

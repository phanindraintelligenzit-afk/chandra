import boto3
from chandra.aws.compliance_models import ComplianceFinding


def scan_cloudtrail():
    findings = []
    cloudtrail = boto3.client('cloudtrail')

    try:
        response = cloudtrail.describe_trails()
        trails = response.get('trailList', [])

        if not trails:
            findings.append(
                ComplianceFinding(
                    kra='Compliance',
                    resource_id='cloudtrail-global',
                    rule_name='CloudTrailEnabled',
                    status='NON_COMPLIANT',
                    finding_details='No CloudTrail configured',
                    severity='CRITICAL',
                    raw_payload=response,
                )
            )
        else:
            # Check if logging is enabled
            for trail in trails:
                trail_name = trail.get('Name', 'unknown')
                is_logging = trail.get('IsLogging', False)
                
                if not is_logging:
                    findings.append(
                        ComplianceFinding(
                            kra='Compliance',
                            resource_id=trail_name,
                            rule_name='CloudTrailLogging',
                            status='NON_COMPLIANT',
                            finding_details=f'CloudTrail {trail_name} logging disabled',
                            severity='CRITICAL',
                            raw_payload=trail,
                        )
                    )
    except Exception as e:
        print(f'Error scanning CloudTrail: {e}')

    return findings

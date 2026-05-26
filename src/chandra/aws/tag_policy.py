import boto3
from chandra.aws.compliance_models import ComplianceFinding

REQUIRED_TAGS = ['Environment', 'Owner', 'Project']


def scan_tag_policy():
    findings = []
    tagging = boto3.client('resourcegroupstaggingapi')

    try:
        response = tagging.get_resources()
        
        for resource in response.get('ResourceTagMappingList', []):
            arn = resource.get('ResourceARN', 'unknown')
            tags = {tag['Key']: tag['Value'] for tag in resource.get('Tags', [])}
            
            for required_tag in REQUIRED_TAGS:
                if required_tag not in tags:
                    findings.append(
                        ComplianceFinding(
                            kra='Compliance',
                            resource_id=arn,
                            rule_name='RequiredTags',
                            status='NON_COMPLIANT',
                            finding_details=f'Missing required tag: {required_tag}',
                            severity='MEDIUM',
                            raw_payload=resource,
                        )
                    )
    except Exception as e:
        print(f'Error scanning tags: {e}')

    return findings

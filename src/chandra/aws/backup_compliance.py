import boto3
from chandra.aws.compliance_models import ComplianceFinding


def scan_backup_compliance():
    findings = []
    backup = boto3.client('backup')

    try:
        response = backup.list_protected_resources()
        resources = response.get('Results', [])

        if not resources:
            findings.append(
                ComplianceFinding(
                    kra='Compliance',
                    resource_id='backup-global',
                    rule_name='BackupProtection',
                    status='NON_COMPLIANT',
                    finding_details='No AWS Backup protected resources found',
                    severity='HIGH',
                    raw_payload=response,
                )
            )
    except Exception as e:
        print(f'Error scanning backup compliance: {e}')

    return findings

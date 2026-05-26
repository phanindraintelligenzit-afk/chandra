import boto3
from chandra.aws.compliance_models import ComplianceFinding


def scan_s3_encryption():
    findings = []
    s3 = boto3.client('s3')

    try:
        buckets = s3.list_buckets().get('Buckets', [])
        
        for bucket in buckets:
            bucket_name = bucket.get('Name', 'unknown')
            try:
                s3.get_bucket_encryption(Bucket=bucket_name)
            except Exception:
                findings.append(
                    ComplianceFinding(
                        kra='Compliance',
                        resource_id=f'arn:aws:s3:::{bucket_name}',
                        rule_name='S3Encryption',
                        status='NON_COMPLIANT',
                        finding_details='S3 bucket encryption disabled',
                        severity='HIGH',
                        raw_payload={'bucket': bucket_name},
                    )
                )
    except Exception as e:
        print(f'Error scanning S3 encryption: {e}')

    return findings


def scan_ebs_encryption():
    findings = []
    ec2 = boto3.client('ec2')

    try:
        response = ec2.get_ebs_encryption_by_default()
        
        if not response.get('EbsEncryptionByDefault', False):
            findings.append(
                ComplianceFinding(
                    kra='Compliance',
                    resource_id='ebs-default-encryption',
                    rule_name='EBSEncryption',
                    status='NON_COMPLIANT',
                    finding_details='Default EBS encryption disabled',
                    severity='HIGH',
                    raw_payload=response,
                )
            )
    except Exception as e:
        print(f'Error scanning EBS encryption: {e}')

    return findings


def scan_rds_encryption():
    findings = []
    rds = boto3.client('rds')

    try:
        response = rds.describe_db_instances()
        
        for db in response.get('DBInstances', []):
            db_id = db.get('DBInstanceIdentifier', 'unknown')
            is_encrypted = db.get('StorageEncrypted', False)
            
            if not is_encrypted:
                findings.append(
                    ComplianceFinding(
                        kra='Compliance',
                        resource_id=f'arn:aws:rds:region:account:db:{db_id}',
                        rule_name='RDSEncryption',
                        status='NON_COMPLIANT',
                        finding_details=f'RDS instance {db_id} encryption disabled',
                        severity='HIGH',
                        raw_payload=db,
                    )
                )
    except Exception as e:
        print(f'Error scanning RDS encryption: {e}')

    return findings

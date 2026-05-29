"""S3, EBS, RDS encryption checkers."""

from __future__ import annotations

import boto3
from chandra.aws.compliance_models import ComplianceFinding


def scan_s3_encryption() -> list[ComplianceFinding]:
    """Scan S3 buckets for encryption."""
    
    client = boto3.client("s3")
    findings: list[ComplianceFinding] = []

    try:
        response = client.list_buckets()
        
        for bucket in response.get("Buckets", []):
            bucket_name = bucket.get("Name", "unknown")
            
            try:
                encryption = client.get_bucket_encryption(Bucket=bucket_name)
                if not encryption or "ServerSideEncryptionConfiguration" not in encryption:
                    findings.append(
                        ComplianceFinding(
                            kra="Compliance",
                            resource_id=f"arn:aws:s3:::{bucket_name}",
                            rule_name="S3Encryption",
                            status="NON_COMPLIANT",
                            finding_details=f"S3 bucket {bucket_name} encryption disabled",
                            severity="HIGH",
                            raw_payload={"bucket": bucket_name},
                        )
                    )
            except client.exceptions.ServerSideEncryptionConfigurationNotFoundError:
                findings.append(
                    ComplianceFinding(
                        kra="Compliance",
                        resource_id=f"arn:aws:s3:::{bucket_name}",
                        rule_name="S3Encryption",
                        status="NON_COMPLIANT",
                        finding_details=f"S3 bucket {bucket_name} encryption disabled",
                        severity="HIGH",
                        raw_payload={"bucket": bucket_name},
                    )
                )

    except Exception as e:
        print(f"Error scanning S3 encryption: {e}")

    return findings


def scan_ebs_encryption() -> list[ComplianceFinding]:
    """Scan EBS encryption by default."""
    
    client = boto3.client("ec2")
    findings: list[ComplianceFinding] = []

    try:
        response = client.get_ebs_encryption_by_default()
        
        if not response.get("EbsEncryptionByDefault", True):
            findings.append(
                ComplianceFinding(
                    kra="Compliance",
                    resource_id="ebs-default",
                    rule_name="EBSEncryption",
                    status="NON_COMPLIANT",
                    finding_details="EBS encryption by default is disabled",
                    severity="HIGH",
                    raw_payload=response,
                )
            )

    except Exception as e:
        print(f"Error scanning EBS encryption: {e}")

    return findings

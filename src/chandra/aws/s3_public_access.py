import boto3
from chandra.aws.security_models import SecurityFinding


def scan_s3_public_access():
    findings = []
    s3 = boto3.client("s3")
    
    try:
        buckets = s3.list_buckets()["Buckets"]
    except Exception as e:
        print(f"Error listing buckets: {e}")
        return findings

    for bucket in buckets:
        bucket_name = bucket["Name"]
        try:
            response = s3.get_public_access_block(Bucket=bucket_name)
            config = response["PublicAccessBlockConfiguration"]

            # If ANY are False, it's a finding
            if not all(config.values()):
                findings.append(
                    SecurityFinding(
                        kra="Security",
                        service="S3",
                        resource_id=f"arn:aws:s3:::{bucket_name}",
                        finding_type="PublicAccessBlockDisabled",
                        severity="CRITICAL",
                        raw_payload=response,
                    )
                )
        except s3.exceptions.NoSuchPublicAccessBlockConfiguration:
            # No public access block = finding
            findings.append(
                SecurityFinding(
                    kra="Security",
                    service="S3",
                    resource_id=f"arn:aws:s3:::{bucket_name}",
                    finding_type="PublicAccessBlockMissing",
                    severity="CRITICAL",
                    raw_payload={},
                )
            )
        except Exception as e:
            print(f"Error scanning bucket {bucket_name}: {e}")

    return findings
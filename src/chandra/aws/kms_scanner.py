import boto3
from chandra.aws.security_models import SecurityFinding


def scan_kms_rotation():
    findings = []
    kms = boto3.client("kms")

    try:
        keys = kms.list_keys()["Keys"]
    except Exception as e:
        print(f"Error listing KMS keys: {e}")
        return findings

    for key in keys:
        key_id = key["KeyId"]
        try:
            rotation = kms.get_key_rotation_status(KeyId=key_id)

            if not rotation["KeyRotationEnabled"]:
                findings.append(
                    SecurityFinding(
                        kra="Security",
                        service="KMS",
                        resource_id=key_id,
                        finding_type="KeyRotationDisabled",
                        severity="HIGH",
                        raw_payload=rotation,
                    )
                )
        except Exception as e:
            print(f"Error scanning key {key_id}: {e}")

    return findings
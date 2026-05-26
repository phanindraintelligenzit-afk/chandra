import boto3
from chandra.aws.security_models import SecurityFinding


def scan_config_rules():
    findings = []
    config = boto3.client("config")

    try:
        rules_response = config.describe_config_rules()
        rules = rules_response.get("ConfigRules", [])
    except Exception as e:
        print(f"Error listing Config Rules: {e}")
        return findings

    for rule in rules:
        rule_name = rule["ConfigRuleName"]
        try:
            response = config.get_compliance_details_by_config_rule(
                ConfigRuleName=rule_name,
                ComplianceTypes=["NON_COMPLIANT"],
            )

            for item in response.get("EvaluationResults", []):
                qualifier = item.get("EvaluationResultIdentifier", {}).get(
                    "EvaluationResultQualifier", {}
                )
                findings.append(
                    SecurityFinding(
                        kra="Security",
                        service="AWSConfig",
                        resource_id=qualifier.get("ResourceId", "unknown"),
                        finding_type="NonCompliantRule",
                        severity="HIGH",
                        raw_payload=item,
                    )
                )
        except Exception as e:
            print(f"Error scanning rule {rule_name}: {e}")

    return findings
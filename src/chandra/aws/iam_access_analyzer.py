import boto3
from chandra.aws.security_models import SecurityFinding


def scan_access_analyzer():
    findings = []
    analyzer = boto3.client("accessanalyzer")

    try:
        analyzers = analyzer.list_analyzers().get("analyzers", [])
    except Exception as e:
        print(f"Error listing Access Analyzers: {e}")
        return findings

    for item in analyzers:
        analyzer_name = item["name"]
        try:
            paginator = analyzer.get_paginator("list_findings")
            pages = paginator.paginate(analyzerName=analyzer_name)

            for page in pages:
                for finding in page.get("findings", []):
                    findings.append(
                        SecurityFinding(
                            kra="Security",
                            service="IAMAccessAnalyzer",
                            resource_id=finding.get("resource", "unknown"),
                            finding_type=finding.get("status", "ACTIVE"),
                            severity="HIGH",
                            raw_payload=finding,
                        )
                    )
        except Exception as e:
            print(f"Error scanning analyzer {analyzer_name}: {e}")

    return findings
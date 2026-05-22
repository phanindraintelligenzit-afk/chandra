import asyncio
import aioboto3
import json
from pathlib import Path
from typing import Optional, Any
from dotenv import load_dotenv

# Framework imports
from agents import Agent, Runner, trace, function_tool

load_dotenv(override=True)

class AWSOptimizationAndSecurityFetcher:
    """
    Bypasses the Trusted Advisor paywall by using AWS Security Hub (for security) 
    and AWS Compute Optimizer (for cost savings). Both are accessible on Basic support plans.
    """

    def __init__(self, max_concurrent: int = 15):
        self._max_concurrent = max_concurrent
        self.__semaphore = None
        self._session = aioboto3.Session()

    @property
    def _semaphore(self) -> asyncio.Semaphore:
        if self.__semaphore is None:
            self.__semaphore = asyncio.Semaphore(self._max_concurrent)
        return self.__semaphore

    async def _list_regions(self) -> list[str]:
        """Return all enabled AWS regions for the current account."""
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            response = await ec2.describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response.get("Regions", []))

    # ------------------------------------------------------------------ #
    #  1. Security Hub (Replaces TA Security Checks)                     #
    # ------------------------------------------------------------------ #

    async def _fetch_region_security(self, region: str) -> list[dict]:
        """Fetches active CRITICAL and HIGH security vulnerabilities from Security Hub."""
        findings = []
        async with self._semaphore:
            try:
                async with self._session.client("securityhub", region_name=region) as sh_client:
                    # Filter for active, critical/high severity issues
                    filters = {
                        'RecordState': [{'Value': 'ACTIVE', 'Comparison': 'EQUALS'}],
                        'SeverityLabel': [
                            {'Value': 'CRITICAL', 'Comparison': 'EQUALS'},
                            {'Value': 'HIGH', 'Comparison': 'EQUALS'}
                        ]
                    }
                    
                    paginator = sh_client.get_paginator("get_findings")
                    async for page in paginator.paginate(Filters=filters):
                        for finding in page.get("Findings", []):
                            findings.append({
                                "Region": region,
                                "Severity": finding.get("Severity", {}).get("Label"),
                                "Title": finding.get("Title"),
                                "Description": finding.get("Description"),
                                "ResourceId": finding.get("Resources", [{}])[0].get("Id", "Unknown"),
                                "Remediation": finding.get("Remediation", {}).get("Recommendation", {}).get("Url", "No URL provided")
                            })
            except Exception as e:
                # Security Hub might not be enabled in this region
                if "InvalidAccessException" in str(e) or "not subscribed" in str(e):
                    pass 
                return []
        return findings

    # ------------------------------------------------------------------ #
    #  2. Compute Optimizer (Replaces TA Cost Optimization)              #
    # ------------------------------------------------------------------ #

    async def _fetch_region_costs(self, region: str) -> list[dict]:
        """Fetches over-provisioned (wasting money) EC2 instances."""
        savings = []
        async with self._semaphore:
            try:
                async with self._session.client("compute-optimizer", region_name=region) as co_client:
                    # Filter for resources that are too big for their workload
                    filters = [{'name': 'Finding', 'values': ['OVER_PROVISIONED']}]
                    
                    response = await co_client.get_ec2_instance_recommendations(filters=filters)
                    
                    for rec in response.get("instanceRecommendations", []):
                        current_type = rec.get("currentInstanceType")
                        options = rec.get("recommendationOptions", [])
                        
                        recommended_type = options[0].get("instanceType") if options else "Unknown"
                        estimated_savings = options[0].get("estimatedMonthlySavings", {}).get("value", 0.0) if options else 0.0

                        savings.append({
                            "Region": region,
                            "ResourceType": "EC2 Instance",
                            "InstanceArn": rec.get("instanceArn"),
                            "Finding": "OVER_PROVISIONED (Wasting Money)",
                            "CurrentType": current_type,
                            "RecommendedType": recommended_type,
                            "EstimatedMonthlySavings": f"${estimated_savings:.2f}"
                        })
            except Exception as e:
                # Compute Optimizer might not be opted-in
                if "OptInRequiredException" in str(e):
                    pass
                return []
        return savings

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_account_audit(self, max_results: int = 20) -> dict[str, Any]:
        """
        Agent-optimized tool. Fetches the highest priority security vulnerabilities
        and cost-saving recommendations across all regions natively.
        """
        regions = await self._list_regions()
        
        # Concurrently fetch security and cost data across all regions
        security_tasks = [self._fetch_region_security(r) for r in regions]
        cost_tasks = [self._fetch_region_costs(r) for r in regions]
        
        all_security_results = await asyncio.gather(*security_tasks, return_exceptions=True)
        all_cost_results = await asyncio.gather(*cost_tasks, return_exceptions=True)

        # Flatten arrays
        security_findings = []
        for res in all_security_results:
            if isinstance(res, list):
                security_findings.extend(res)
                
        cost_findings = []
        for res in all_cost_results:
            if isinstance(res, list):
                cost_findings.extend(res)

        return {
            "AuditSource": "AWS Security Hub & AWS Compute Optimizer",
            "SecurityVulnerabilities": {
                "TotalCriticalOrHigh": len(security_findings),
                "TopIssues": security_findings[:max_results]
            },
            "CostOptimizations": {
                "TotalOverProvisionedResources": len(cost_findings),
                "TopSavings": cost_findings[:max_results]
            }
        }


# ================================================================== #
#  Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSOptimizationAndSecurityFetcher()
    
#     print("\n--- Running Free-Tier Accessible Audit ---")
#     report = await fetcher.fetch_account_audit()
    
#     output_path = Path("observation") / "aws_account_audit.json"
#     output_path.parent.mkdir(parents=True, exist_ok=True)
#     output_path.write_text(json.dumps(report, indent=4), encoding="utf-8")
    
#     print(json.dumps(report, indent=4))
#     print(f"\n✅ Successfully written audit report to {output_path}")

# async def run_agent_observation() -> None:
#     fetcher = AWSOptimizationAndSecurityFetcher()
    
#     tools = [function_tool(fetcher.fetch_account_audit)]
    
#     prompt = (
#         "You are an AWS Cloud Optimization Agent. "
#         "Review the Security Hub and Compute Optimizer audit. "
#         "If there are no results, advise the user to enable Security Hub and Compute Optimizer in their AWS Console. "
#         "Otherwise, summarize the critical security vulnerabilities and point out the resources wasting the most money."
#     )
    
#     agent = Agent(name="Chandra", instructions=prompt, model="gpt-5.4-nano", tools=tools)
    
#     result = await Runner.run(
#         agent,
#         "Run a security and cost audit on my account. What are the most critical errors or cost savings I need to address?"
#     )
#     print("\n--- Agent Final Output ---")
#     print(result.final_output)


# if __name__ == "__main__":
#     asyncio.run(main())
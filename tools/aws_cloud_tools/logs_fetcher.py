import asyncio
import aioboto3
import json
from pathlib import Path
from typing import Optional, Any
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Framework imports (Uncomment when running in your pipeline)
from agents import Agent, Runner, trace, function_tool

load_dotenv(override=True)

class AWSCloudWatchLogsFetcher:
    """
    Asynchronously executes CloudWatch Logs Insights queries across all regions.
    Includes built-in error prioritization.
    
    Requires: pip install aioboto3
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
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            response = await ec2.describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response.get("Regions", []))

    async def _execute_query_in_region(
        self, region: str, log_group_prefix: Optional[str], query: str, start_time: int, end_time: int
    ) -> list[dict]:
        async with self._semaphore:
            try:
                async with self._session.client("logs", region_name=region) as logs_client:
                    # 1. Discover log groups (up to 50 allowed by AWS Insights)
                    paginator = logs_client.get_paginator("describe_log_groups")
                    log_group_names = []
                    kwargs = {"logGroupNamePrefix": log_group_prefix} if log_group_prefix else {}
                    
                    async for page in paginator.paginate(**kwargs):
                        for lg in page.get("logGroups", []):
                            log_group_names.append(lg["logGroupName"])
                            if len(log_group_names) >= 50: break
                        if len(log_group_names) >= 50: break
                            
                    if not log_group_names: return []

                    # 2. Start query
                    start_response = await logs_client.start_query(
                        logGroupNames=log_group_names,
                        startTime=start_time,
                        endTime=end_time,
                        queryString=query
                    )
                    query_id = start_response["queryId"]
                    
                    # 3. Poll for completion
                    while True:
                        await asyncio.sleep(2)
                        res = await logs_client.get_query_results(queryId=query_id)
                        if res["status"] in ["Complete", "Failed", "Cancelled"]:
                            break
                    
                    if res["status"] != "Complete": return []
                        
                    return [{"Region": region, **{field["field"]: field["value"] for field in row}} 
                            for row in res.get("results", [])]
            except Exception:
                return []

    async def fetch_critical_errors(
        self, log_group_prefix: Optional[str] = None, hours_lookback: int = 24, max_results: int = 50
    ) -> dict[str, Any]:
        """High-priority tool: Specifically scans for ERROR, Exception, or Critical logs."""
        query = (
            "fields @timestamp, @message, @logStream "
            "| filter @message like /(?i)(ERROR|Exception|Critical|Fail)/ "
            "| sort @timestamp desc "
            "| limit %d" % max_results
        )
        return await self.fetch_global_logs(log_group_prefix, query, hours_lookback, max_results)

    async def fetch_global_logs(
        self, log_group_prefix: Optional[str] = None, 
        query: str = "fields @timestamp, @message | sort @timestamp desc | limit 50", 
        hours_lookback: int = 24, 
        max_total_results: int = 20
    ) -> dict[str, Any]:
        """Generic tool: Runs a custom Insights query across all regions."""
        end_time = int(datetime.now(timezone.utc).timestamp())
        start_time = int((datetime.now(timezone.utc) - timedelta(hours=hours_lookback)).timestamp())
        
        regions = await self._list_regions()
        results = await asyncio.gather(
            *[self._execute_query_in_region(r, log_group_prefix, query, start_time, end_time) for r in regions],
            return_exceptions=True
        )

        combined = [item for sublist in results if isinstance(sublist, list) for item in sublist]
        combined.sort(key=lambda x: x.get("@timestamp", ""), reverse=True)

        return {
            "QueryExecuted": query,
            "TotalMatchesGlobal": len(combined),
            "Results": combined[:max_total_results]
        }

# ================================================================== #
# Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSCloudWatchLogsFetcher()
    
#     # Example 1: Scan for errors across EVERYTHING (High Priority)
#     print("\n--- Running High Priority Error Scan ---")
#     error_report = await fetcher.fetch_critical_errors(max_results=50)
#     print(json.dumps(error_report, indent=4))
    
#     # Example 2: General scan
#     print("\n--- Running General Log Scan ---")
#     gen_report = await fetcher.fetch_global_logs(
#         log_group_prefix=None, 
#         query="fields @timestamp, @message | sort @timestamp desc | limit 50"
#     )
#     print(json.dumps(gen_report, indent=4))

# async def run_agent_observation() -> None:
#     fetcher = AWSCloudWatchLogsFetcher()
#     tools = [function_tool(fetcher.fetch_critical_errors), function_tool(fetcher.fetch_global_logs)]
    
#     agent = Agent(
#         name="Chandra", 
#         instructions="Use 'fetch_critical_errors' for triage. Use 'fetch_global_logs' for deep dives.", 
#         model="gpt-5.4-nano", 
#         tools=tools
#     )
    
#     result = await Runner.run(agent, "Check my logs for recent errors.")
#     print(result.final_output)

# if __name__ == "__main__":
#     asyncio.run(main())
#     asyncio.run(run_agent_observation())
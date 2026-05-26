import asyncio
import aioboto3
import json
import pytz
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Any
from dotenv import load_dotenv

# Framework imports (Uncomment when running in your pipeline)
from agents import Agent, Runner, trace, function_tool

load_dotenv(override=True)

class AWSXRayFetcher:
    """
    Asynchronously fetches AWS X-Ray trace summaries across ALL enabled regions.
    
    Requires: pip install aioboto3 pytz
    """

    def __init__(self, timezone: str = "Asia/Kolkata"):
        self._tz = pytz.timezone(timezone)
        self._session = aioboto3.Session()

    # ------------------------------------------------------------------ #
    #  Private Helpers                                                   #
    # ------------------------------------------------------------------ #

    async def _list_regions(self) -> list[str]:
        """Return all enabled AWS regions for the current account."""
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            response = await ec2.describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response.get("Regions", []))

    def _parse_trace(self, trace: dict, region: str) -> dict:
        """
        Cleans up raw X-Ray traces, adds region tracking, and calculates a human-readable Status.
        """
        status = "Success (200)"
        if trace.get("HasFault"):
            status = "Fault (5xx)"
        elif trace.get("HasError"):
            status = "Error (4xx)"
        elif trace.get("IsPartial"):
            status = "Partial/Incomplete"

        http_meta = trace.get("Http", {})
        
        parsed = {
            "Region": region,  # Track which region served the request
            "Id": trace.get("Id"),
            "Status": status,
            "DurationSeconds": round(trace.get("Duration", 0), 4),
            "Method": http_meta.get("HttpMethod", "UNKNOWN"),
            "URL": http_meta.get("HttpURL", "UNKNOWN"),
            "ClientIP": http_meta.get("ClientIp", "UNKNOWN"),
            "ResponseStatus": http_meta.get("HttpStatus"),
        }

        if trace.get("EntryPoint"):
            parsed["EntryPoint"] = trace["EntryPoint"].get("Name")
            
        for time_field in ["StartTime", "EndTime"]:
            if trace.get(time_field):
                parsed[time_field] = trace[time_field].astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z")

        return parsed

    async def _fetch_region_trace_summaries(self, region: str, start_time: datetime, end_time: datetime) -> list[dict]:
        """
        Worker function to fetch full, paginated X-Ray trace summaries for a single region.
        """
        region_traces = []

        try:
            async with self._session.client("xray", region_name=region) as xray:
                paginator = xray.get_paginator("get_trace_summaries")

                async for page in paginator.paginate(StartTime=start_time, EndTime=end_time):
                    for trace in page.get("TraceSummaries", []):
                        region_traces.append(self._parse_trace(trace, region))
        except Exception:
            # X-Ray might not be fully configured in every single active region; skip errors silently
            return []

        return region_traces

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_all_regions_raw_traces(
        self, 
        hours_lookback: int = 8, 
        regions: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Fetches X-Ray trace summaries across all specified regions concurrently.
        """
        end_time = datetime.now(pytz.utc)
        start_time = end_time - timedelta(hours=hours_lookback)
        
        target_regions = regions or await self._list_regions()
        
        # Fire concurrent requests to all regions
        region_results = await asyncio.gather(
            *[
                self._fetch_region_trace_summaries(region=r, start_time=start_time, end_time=end_time)
                for r in target_regions
            ],
            return_exceptions=True,
        )

        combined_traces = []
        for result in region_results:
            if isinstance(result, list):
                combined_traces.extend(result)

        # Sort globally chronologically, newest first
        combined_traces.sort(key=lambda x: x.get("StartTime", ""), reverse=True)
        return combined_traces

    async def fetch_global_xray_summary(
        self, 
        hours_lookback: int = 8, 
        only_errors: bool = False,
        max_traces_total: int = 50
    ) -> dict[str, Any]:
        """
        Fetch a compact global X-Ray summary optimized for LLM context windows.
        Aggregates health metrics across all regions natively.
        """
        raw_traces = await self.fetch_all_regions_raw_traces(hours_lookback=hours_lookback)
        
        filtered_traces = []
        global_metrics = {
            "TotalRequests": len(raw_traces),
            "SuccessCount": 0,
            "ErrorCount_4xx": 0,
            "FaultCount_5xx": 0,
            "TracesByRegion": {}
        }

        for trace in raw_traces:
            region = trace["Region"]
            
            # Initialize regional grouping
            if region not in global_metrics["TracesByRegion"]:
                global_metrics["TracesByRegion"][region] = {"Total": 0, "Faults": 0, "Errors": 0}
                
            global_metrics["TracesByRegion"][region]["Total"] += 1

            # Tally metrics globally and per-region
            if "Fault (5xx)" in trace["Status"]:
                global_metrics["FaultCount_5xx"] += 1
                global_metrics["TracesByRegion"][region]["Faults"] += 1
            elif "Error (4xx)" in trace["Status"]:
                global_metrics["ErrorCount_4xx"] += 1
                global_metrics["TracesByRegion"][region]["Errors"] += 1
            else:
                global_metrics["SuccessCount"] += 1

            # Apply LLM filtering logic
            if only_errors and "Success" in trace["Status"]:
                continue
                
            filtered_traces.append(trace)

        summary = {
            "LookbackHours": hours_lookback,
            "Filtering": "Errors & Faults Only" if only_errors else "All Traces",
            "HealthMetrics": global_metrics,
            "LatestTraces": filtered_traces[:max_traces_total]
        }
        
        return summary


# ================================================================== #
#  Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSXRayFetcher()
    
#     # 1. Export comprehensive raw multi-region trace data locally
#     report = await fetcher.fetch_all_regions_raw_traces(hours_lookback=8)
#     output_path = Path("observation") / "xray_traces_raw.json"
#     output_path.parent.mkdir(parents=True, exist_ok=True)
#     output_path.write_text(json.dumps(report, indent=4), encoding="utf-8")
#     print(f"✅ Successfully written full global X-Ray report to {output_path}")

#     # 2. Print a clean summary for verification
#     summary = await fetcher.fetch_global_xray_summary(hours_lookback=8, only_errors=False)
#     print("\n--- Global X-Ray Trace Summary ---")
#     print(json.dumps(summary, indent=4))


# async def run_agent_observation() -> None:
#     fetcher = AWSXRayFetcher()
    
#     tools = [function_tool(fetcher.fetch_global_xray_summary)]
    
#     prompt = (
#         "You are a global Cloud Application Performance Monitoring (APM) Agent. "
#         "Review the multi-region X-Ray traces to identify bottlenecks, identify which regions "
#         "are experiencing the highest fault rates (5xx), and summarize failing endpoints."
#     )
    
#     agent = Agent(name="Chandra", instructions=prompt, model="gpt-5.4-nano", tools=tools)
    
#     result = await Runner.run(
#         agent,
#         "Fetch my application traces across all regions for the last 8 hours. Are any regions experiencing higher error rates than others?"
#     )
#     print("\n--- Agent Final Output ---")
#     print(result.final_output)


# if __name__ == "__main__":
#     asyncio.run(main())
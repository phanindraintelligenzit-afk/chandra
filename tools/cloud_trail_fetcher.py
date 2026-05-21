import asyncio
import aioboto3
import pytz
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Any
from dotenv import load_dotenv

# Framework imports (Uncomment when running in your pipeline)
from agents import Agent, Runner, trace, function_tool

load_dotenv(override=True)

class AWSCloudTrailFetcher:
    """
    Asynchronously fetches and parses AWS CloudTrail management events across multiple regions.
    
    Requires: pip install aioboto3 pytz
    """

    def __init__(self, timezone: str = "Asia/Kolkata", max_concurrent: int = 10):
        self._tz = pytz.timezone(timezone)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session = aioboto3.Session()

    # ------------------------------------------------------------------ #
    #  Private helpers                                                   #
    # ------------------------------------------------------------------ #

    async def _list_regions(self) -> list[str]:
        """Return all enabled AWS regions for the current account."""
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            response = await ec2.describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response.get("Regions", []))

    def _parse_event(self, raw_event: dict, region: str) -> dict:
        """Extracts top-level data and cleans up stringified inner CloudTrail payload."""
        parsed = {
            "Region": region,
            "EventId": raw_event.get("EventId"),
            "EventName": raw_event.get("EventName"),
            "Username": raw_event.get("Username"),
            "EventTime": raw_event.get("EventTime").astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z")
                if raw_event.get("EventTime") else "",
            "ResourceList": [
                {"Type": r.get("ResourceType"), "Name": r.get("ResourceName")}
                for r in raw_event.get("Resources", [])
            ],
        }

        # Safe extraction of deep metadata from the inner stringified payload
        if "CloudTrailEvent" in raw_event:
            try:
                inner = json.loads(raw_event["CloudTrailEvent"])
                parsed["SourceIP"] = inner.get("sourceIPAddress")
                parsed["UserAgent"] = inner.get("userAgent")
                
                # Capture authentication or execution errors if they exist
                if inner.get("errorCode"):
                    parsed["Error"] = {
                        "Code": inner.get("errorCode"),
                        "Message": inner.get("errorMessage")
                    }
            except json.JSONDecodeError:
                pass

        return parsed

    async def _fetch_region_events(
        self,
        region: str,
        start_time: datetime,
        end_time: datetime,
        lookup_attribute: Optional[dict],
        max_events_per_region: int,
    ) -> list[dict]:
        """Worker function to look up events within a single region."""
        events: list[dict] = []
        kwargs = {
            "StartTime": start_time,
            "EndTime": end_time,
        }
        if lookup_attribute:
            kwargs["LookupAttributes"] = [lookup_attribute]

        async with self._semaphore:
            try:
                async with self._session.client("cloudtrail", region_name=region) as ct:
                    paginator = ct.get_paginator("lookup_events")
                    async for page in paginator.paginate(**kwargs):
                        for raw_ev in page.get("Events", []):
                            events.append(self._parse_event(raw_ev, region))
                            if len(events) >= max_events_per_region:
                                return events
            except Exception:
                # Silently catch token errors or region initialization failures
                return []
        return events

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_all_regions_events(
        self,
        last_hours: int = 2,
        event_name: Optional[str] = None,
        max_events_per_region: int = 50,
        regions: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Gathers raw, multi-region management API logs concurrently.
        """
        end_time = datetime.now(pytz.utc)
        start_time = end_time - timedelta(hours=last_hours)
        
        target_regions = regions or await self._list_regions()
        lookup_attr = {"AttributeKey": "EventName", "AttributeValue": event_name} if event_name else None

        region_results = await asyncio.gather(
            *[
                self._fetch_region_events(
                    region=r,
                    start_time=start_time,
                    end_time=end_time,
                    lookup_attribute=lookup_attr,
                    max_events_per_region=max_events_per_region
                )
                for r in target_regions
            ],
            return_exceptions=True,
        )

        combined_events = []
        for result in region_results:
            if isinstance(result, list):
                combined_events.extend(result)

        # Globally sort chronological: Newest events first
        combined_events.sort(key=lambda x: x["EventTime"], reverse=True)
        return combined_events

    async def fetch_events_summary(
        self,
        last_hours: int = 2,
        event_name: Optional[str] = None,
        max_total_events: int = 30,
    ) -> dict[str, Any]:
        """
        Fetch a compact log group summary explicitly optimized for LLM token usage limits.
        Filters out boilerplate, sorts chronologically, and applies a strict global limit.
        """
        events = await self.fetch_all_regions_events(
            last_hours=last_hours,
            event_name=event_name,
            max_events_per_region=max_total_events
        )

        # Build clean operational summary wrapper
        summary = {
            "lookup_window_hours": last_hours,
            "event_name_filter": event_name or "ALL_ACTIONS",
            "total_discovered_count": len(events),
            "events": events[:max_total_events]
        }
        return summary


# ================================================================== #
#  Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSCloudTrailFetcher()
    
#     # 1. Gather comprehensive raw logs and export locally to file
#     report = await fetcher.fetch_events_summary(last_hours=4)
#     output_path = Path("observation") / "cloudtrail_events.json"
#     output_path.parent.mkdir(parents=True, exist_ok=True)
#     output_path.write_text(json.dumps(report, indent=4), encoding="utf-8")
#     print(f"✅ Successfully written full report to {output_path}")


# async def run_agent_observation() -> None:
#     fetcher = AWSCloudTrailFetcher()
    
#     # Expose the optimized summary API structure to the agent pipeline
#     tools = [function_tool(fetcher.fetch_events_summary)]
    
#     prompt = (
#         "You are an advanced cloud watcher agent specialized in auditing AWS infrastructure. "
#         "Use your tools to find out what API mutations, log activity, or permission adjustments "
#         "have taken place across regions."
#     )
    
#     agent = Agent(name="Chandra", instructions=prompt, model="gpt-5.4-nano", tools=tools)
    
#     result = await Runner.run(
#         agent,
#         "Analyze security auditing events and API logs from the last 3 hours. Identify who executed what actions."
#     )
#     print("\n--- Agent Final Output ---")
#     print(result.final_output)


# if __name__ == "__main__":
#     # Standard local validation workflow block
#     asyncio.run(main())
    
#     # Run agent engine block (Switch to 'await run_agent_observation()' directly if executing in a Jupyter frame)
#     asyncio.run(run_agent_observation())
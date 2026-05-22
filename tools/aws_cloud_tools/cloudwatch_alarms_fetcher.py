import asyncio
import aioboto3
import json
from pathlib import Path
from typing import Optional, Any
from datetime import datetime
from dotenv import load_dotenv

# Framework imports (Uncomment when running in your pipeline)
from agents import Agent, Runner, trace, function_tool

load_dotenv(override=True)

class AWSCloudWatchAlarmsFetcher:
    """
    Asynchronously fetches AWS CloudWatch Alarms across all enabled regions.
    Surfaces all created alarms in the account across all status states.
    
    Requires: pip install aioboto3
    """

    def __init__(self):
        self._session = aioboto3.Session()

    # ------------------------------------------------------------------ #
    #  Private Helpers                                                   #
    # ------------------------------------------------------------------ #

    async def _list_regions(self) -> list[str]:
        """Return all enabled AWS regions for the current account."""
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            response = await ec2.describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response.get("Regions", []))

    def _parse_alarm(self, alarm: dict, region: str, alarm_type: str) -> dict:
        """Cleans up the raw CloudWatch alarm payload for the LLM."""
        
        # Datetime objects need to be converted to strings for JSON serialization
        updated_time = alarm.get("StateUpdatedTimestamp")
        if isinstance(updated_time, datetime):
            updated_time = updated_time.strftime("%Y-%m-%d %H:%M:%S UTC")

        parsed = {
            "Region": region,
            "Type": alarm_type,  # 'Metric' or 'Composite'
            "AlarmName": alarm.get("AlarmName"),
            "State": alarm.get("StateValue"),
            "StateReason": alarm.get("StateReason"),
            "LastUpdated": updated_time,
            "Description": alarm.get("AlarmDescription", "No description provided")
        }

        # Metric alarms have specific metric data; Composite alarms are just logical groups
        if alarm_type == "Metric":
            parsed["Namespace"] = alarm.get("Namespace", "Unknown")
            parsed["MetricName"] = alarm.get("MetricName", "Unknown")

        return parsed

    async def _fetch_region_alarms(self, region: str, state_filter: Optional[str] = None) -> list[dict]:
        """
        Worker function to fetch paginated CloudWatch alarms for a specific region.
        """
        region_alarms = []
        
        # Build arguments dynamically. If state_filter is None, it fetches all created alarms.
        kwargs = {}
        if state_filter:
            kwargs["StateValue"] = state_filter

        try:
            async with self._session.client("cloudwatch", region_name=region) as cw_client:
                paginator = cw_client.get_paginator("describe_alarms")

                async for page in paginator.paginate(**kwargs):
                    # CloudWatch splits alarms into standard Metric Alarms and Composite Alarms
                    for metric_alarm in page.get("MetricAlarms", []):
                        region_alarms.append(self._parse_alarm(metric_alarm, region, "Metric"))

                    for composite_alarm in page.get("CompositeAlarms", []):
                        region_alarms.append(self._parse_alarm(composite_alarm, region, "Composite"))

        except Exception:
            # Silently catch token errors or region initialization failures
            return []

        return region_alarms

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #

    async def fetch_all_regions_alarms(
        self, 
        state_filter: Optional[str] = None, 
        regions: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Gathers raw alarm configurations concurrently across regions.
        Defaults to pulling ALL created alarms (state_filter=None).
        """
        target_regions = regions or await self._list_regions()

        region_results = await asyncio.gather(
            *[
                self._fetch_region_alarms(region=r, state_filter=state_filter)
                for r in target_regions
            ],
            return_exceptions=True,
        )

        combined_alarms = []
        for result in region_results:
            if isinstance(result, list):
                combined_alarms.extend(result)

        # Sort so the most recently modified alarms appear at the top
        combined_alarms.sort(key=lambda x: x.get("LastUpdated", ""), reverse=True)
        return combined_alarms

    async def fetch_alarms_summary(
        self, 
        state_filter: Optional[str] = None, 
        max_total_alarms: int = 40
    ) -> dict[str, Any]:
        """
        Agent-optimized tool. Fetches created CloudWatch alarms across all regions, 
        capped to protect the LLM context window from massive alert lists.
        """
        alarms = await self.fetch_all_regions_alarms(state_filter=state_filter)

        summary = {
            "FilterApplied": f"Alarms currently in state: {state_filter}" if state_filter else "ALL_CREATED_ALARMS",
            "TotalAlarmsFoundGlobal": len(alarms),
            "Alarms": alarms[:max_total_alarms]
        }
        
        return summary


# ================================================================== #
#  Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSCloudWatchAlarmsFetcher()
    
#     # 1. Gather ALL created alarms in the account across all states
#     print("\n--- Fetching All Created CloudWatch Alarms Across Regions ---")
#     report = await fetcher.fetch_all_regions_alarms(state_filter=None)
    
#     output_path = Path("observation") / "cloudwatch_all_created_alarms.json"
#     output_path.parent.mkdir(parents=True, exist_ok=True)
#     output_path.write_text(json.dumps(report, indent=4), encoding="utf-8")
    
#     print(json.dumps(report, indent=4))
#     print(f"\n✅ Successfully written full CloudWatch inventory report to {output_path}")


# async def run_agent_observation() -> None:
#     fetcher = AWSCloudWatchAlarmsFetcher()
    
#     # Expose the optimized summary API structure to the agent pipeline
#     tools = [function_tool(fetcher.fetch_alarms_summary)]
    
#     prompt = (
#         "You are an AWS Site Reliability Engineer (SRE) Agent. "
#         "Review the complete list of created CloudWatch alarms. "
#         "Summarize account health by highlighting which alarms are in an ALARM state, "
#         "which ones are currently healthy (OK), and if any are missing metrics (INSUFFICIENT_DATA)."
#     )
    
#     agent = Agent(name="Chandra", instructions=prompt, model="gpt-5.4-nano", tools=tools)
    
#     # The agent can explicitly inspect all alarms or decide to query a specific subset
#     result = await Runner.run(
#         agent,
#         "Fetch an inventory of all created CloudWatch alarms globally. What is the status distribution?"
#     )
#     print("\n--- Agent Final Output ---")
#     print(result.final_output)


# if __name__ == "__main__":
#     # Standard local validation workflow block
#     asyncio.run(main())

#     asyncio.run(run_agent_observation())
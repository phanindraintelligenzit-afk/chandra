import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import aioboto3

# Framework imports (Uncomment when running in your pipeline)
from dotenv import load_dotenv

load_dotenv(override=True)

class AWSConfigHistoryFetcher:
    """
    Asynchronously tracks actual AWS configuration changes and deletions across 
    ALL discovered resource types globally using native Config History APIs.
    
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

    async def _check_region_recorder(self, region: str) -> dict:
        """Checks if AWS Config is actually turned on in a region."""
        try:
            async with self._session.client("config", region_name=region) as config_client:
                response = await config_client.describe_configuration_recorder_status()
                recorders = response.get("ConfigurationRecordersStatus", [])

                if not recorders:
                    return {"Region": region, "Status": "OFF (No Recorder Configured)"}

                status = recorders[0]
                is_recording = status.get("recording", False)
                last_status = status.get("lastStatus", "UNKNOWN")

                return {
                    "Region": region,
                    "Status": "RECORDING" if is_recording else "OFF",
                    "DeliveryStatus": last_status
                }
        except Exception as e:
            return {"Region": region, "Status": f"ERROR: {e!s}"}

    async def _get_active_resource_types(self, config_client) -> list[str]:
        """
        Queries AWS Config to discover which resource types actually exist 
        in this region so we don't scan empty resource types.
        """
        active_types = []
        try:
            paginator = config_client.get_paginator("get_discovered_resource_counts")
            async for page in paginator.paginate():
                for item in page.get("resourceCounts", []):
                    if item.get("count", 0) > 0:
                        active_types.append(item.get("resourceType"))
        except Exception:
            pass
        return active_types

    async def _get_resource_diff(
        self, 
        config_client, 
        region: str, 
        resource_type: str, 
        resource_id: str, 
        start_time: datetime, 
        end_time: datetime
    ) -> dict | None:
        """
        Fetches the configuration history of a single resource in the specified time window.
        Returns the before/after state so the LLM knows EXACTLY what changed.
        """
        try:
            response = await config_client.get_resource_config_history(
                resourceType=resource_type,
                resourceId=resource_id,
                laterTime=end_time,
                earlierTime=start_time,
                limit=50  # Grab tracking list items up to bounds
            )
            items = response.get("configurationItems", [])
            
            # If nothing was returned, the resource didn't change in this time window
            if not items:
                return None
            
            latest = items[0]
            status = latest.get("configurationItemStatus")
            
            result = {
                "Region": region,
                "ResourceType": resource_type,
                "ResourceId": resource_id,
                "Status": status,
                "CaptureTime": str(latest.get("configurationItemCaptureTime")),
            }

            # Handle Deletions (We grab the previous state to see what was destroyed)
            if status == "ResourceDeleted":
                result["Action"] = "DELETED"
                if len(items) > 1:
                    result["DeletedConfiguration"] = items[1].get("configuration")
            
            # Handle Modifications (We grab both states to diff them)
            elif len(items) == 2:
                result["Action"] = "MODIFIED"
                result["CurrentConfiguration"] = latest.get("configuration")
                result["PreviousConfiguration"] = items[1].get("configuration")
            
            # Handle Creations
            else:
                result["Action"] = "CREATED_OR_UPDATED"
                result["CurrentConfiguration"] = latest.get("configuration")
                
            return result
            
        except Exception:
            return None

    async def _scan_region_for_changes(
        self, 
        region: str, 
        start_time: datetime, 
        end_time: datetime
    ) -> list[dict]:
        """
        Worker function: Dynamically discovers active resource types in the region,
        lists all matching resource identifiers, and collects their changes.
        """
        region_changes = []

        try:
            async with self._session.client("config", region_name=region) as config_client:
                # 1. Dynamically discover active types in this region
                active_resource_types = await self._get_active_resource_types(config_client)

                # 2. Iterate through only the active resource types found
                for resource_type in active_resource_types:
                    paginator = config_client.get_paginator("list_discovered_resources")

                    async for page in paginator.paginate(resourceType=resource_type, includeDeletedResources=True):
                        resource_ids = [res["resourceId"] for res in page.get("resourceIdentifiers", [])]

                        if not resource_ids:
                            continue

                        # 3. Fetch history/diff profile concurrently for discovered items
                        history_tasks = [
                            self._get_resource_diff(config_client, region, resource_type, r_id, start_time, end_time)
                            for r_id in resource_ids
                        ]

                        results = await asyncio.gather(*history_tasks)

                        for res in results:
                            if res:
                                region_changes.append(res)

        except Exception:
            return []

        return region_changes

    # ------------------------------------------------------------------ #
    #  Public API                                                        #
    # ------------------------------------------------------------------ #

    async def check_recorder_status(self) -> dict:
        """
        Diagnostic tool: Checks if AWS Config is actually turned on across all regions.
        If this returns OFF for a region, no history or changes can be queried there.
        """
        regions = await self._list_regions()
        
        status_results = await asyncio.gather(
            *[self._check_region_recorder(r) for r in regions]
        )
        
        return {"ConfigRecorderStatus": status_results}

    async def fetch_recent_changes(
        self, 
        hours_lookback: int = 24, 
        max_total_results: int = 15
    ) -> dict[str, Any]:
        """
        Agent-optimized tool. Natively scans all regions for ALL active resource types
        and extracts the exact before/after configuration differences.
        """
        # AWS Config History API requires native timezone-aware datetime objects
        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(hours=hours_lookback)
        
        regions = await self._list_regions()
        
        region_results = await asyncio.gather(
            *[
                self._scan_region_for_changes(r, start_time, end_time) 
                for r in regions
            ],
            return_exceptions=True
        )

        combined_results = []
        for result in region_results:
            if isinstance(result, list):
                combined_results.extend(result)

        # Sort globally chronologically by capture time (Newest first)
        combined_results.sort(key=lambda x: x.get("CaptureTime", ""), reverse=True)

        sliced_results = combined_results[:max_total_results]

        return {
            "TimeWindowHours": hours_lookback,
            "ResourceTypeFilter": "ALL_DISCOVERED_TYPES",
            "TotalChangesFoundGlobal": len(combined_results),
            "ChangeLogs": sliced_results
        }


# ================================================================== #
#  Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSConfigHistoryFetcher()
    
#     # 1. DIAGNOSTIC: Check if AWS Config is even turned on
#     print("\n--- Checking AWS Config Recorder Status ---")
#     status = await fetcher.check_recorder_status()
#     print(json.dumps(status, indent=4))
    
#     # 2. Fetch actual historical changes for ALL resource types
#     print("\n--- Fetching Changes Across All Resource Types (Last 24 Hours) ---")
#     report = await fetcher.fetch_recent_changes(hours_lookback=24)
#     print(json.dumps(report, indent=4))


# async def run_agent_observation() -> None:
#     fetcher = AWSConfigHistoryFetcher()
    
#     tools = [
#         function_tool(fetcher.check_recorder_status),
#         function_tool(fetcher.fetch_recent_changes)
#     ]
    
#     prompt = (
#         "You are an Infrastructure Drift Agent. "
#         "If you cannot find any data, check the recorder status to see if AWS Config is even turned on. "
#         "Review the AWS Config history logs, compare the 'PreviousConfiguration' to the 'CurrentConfiguration', "
#         "and summarize exactly what properties changed or were deleted across all services."
#     )
    
#     agent = Agent(name="Chandra", instructions=prompt, model="gpt-5.4-nano", tools=tools)
    
#     result = await Runner.run(
#         agent,
#         "Check if my Config Recorder is running, then tell me what infrastructure resource components changed globally in the last 24 hours."
#     )
#     print("\n--- Agent Final Output ---")
#     print(result.final_output)


# if __name__ == "__main__":
#     asyncio.run(main())
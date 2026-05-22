import asyncio
import aioboto3
import json
from typing import Optional, Any
from dotenv import load_dotenv

# Framework imports
from agents import Agent, Runner, function_tool

load_dotenv(override=True)

class AWSHealthEventsFetcher:
    """
    Asynchronously fetches AWS Health events (incidents, scheduled changes).
    Requires: Business or Enterprise Support plan.
    """

    def __init__(self):
        # Health API is global and is served from us-east-1
        self._session = aioboto3.Session(region_name="us-east-1")

    async def fetch_recent_events(self, max_results: int = 20) -> dict[str, Any]:
        """
        Fetches currently open issues or scheduled changes globally.
        """
        async with self._session.client("health") as health:
            try:
                # Filter for 'open' or 'upcoming' events
                response = await health.describe_events(
                    filter={
                        "eventStatusCodes": ["open", "upcoming"],
                    },
                    maxResults=max_results
                )
                
                events = response.get("events", [])
                if not events:
                    return {"Status": "No active issues or scheduled changes found."}

                # Get details for these events (descriptions, etc.)
                event_arns = [e["arn"] for e in events]
                details_response = await health.describe_event_details(eventArns=event_arns)
                
                # Format for the LLM
                formatted_events = []
                for detail in details_response.get("successfulSet", []):
                    event = detail.get("event", {})
                    desc = detail.get("eventDescription", {}).get("latestDescription", "")
                    
                    formatted_events.append({
                        "Service": event.get("service"),
                        "Region": event.get("region"),
                        "Status": event.get("statusCode"),
                        "Type": event.get("eventTypeCode"),
                        "StartTime": str(event.get("startTime")),
                        "Description": desc[:200] + "..." # Truncate for token efficiency
                    })
                
                return {"Events": formatted_events}
                
            except Exception as e:
                return {"Error": f"Failed to fetch health events: {str(e)}"}

# ================================================================== #
# Agent Integration                                                  #
# ================================================================== #

# async def main():
#     fetcher = AWSHealthEventsFetcher()
#     print("\n--- Fetching AWS Health Events ---")
#     print(json.dumps(await fetcher.fetch_recent_events(), indent=4))

# async def run_agent_observation() -> None:
#     fetcher = AWSHealthEventsFetcher()
#     tools = [function_tool(fetcher.fetch_recent_events)]
    
#     agent = Agent(
#         name="Chandra", 
#         instructions="Check for any AWS service disruptions or maintenance that might affect the account.", 
#         model="gpt-5.4-nano", 
#         tools=tools
#     )
    
#     result = await Runner.run(agent, "Are there any current AWS service incidents?")
#     print(result.final_output)

# if __name__ == "__main__":
#     asyncio.run(main())
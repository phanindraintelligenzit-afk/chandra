import asyncio
import aioboto3
import json
from typing import Optional, Any
from dotenv import load_dotenv

# Framework imports
from agents import Agent, Runner, trace, function_tool

load_dotenv(override=True)

class AWSBudgetsFetcher:
    """
    Asynchronously fetches AWS Budgets and current spend status.
    Requires: pip install aioboto3
    """

    def __init__(self):
        self._session = aioboto3.Session()

    async def _get_account_id(self, budgets_client) -> str:
        """Resolves the current AWS Account ID."""
        async with self._session.client("sts") as sts:
            identity = await sts.get_caller_identity()
            return identity["Account"]

    async def fetch_budget_status(self) -> dict[str, Any]:
        """
        Agent-optimized tool. Fetches all configured budgets and compares 
        'ActualSpend' against 'BudgetLimit'.
        """
        async with self._session.client("budgets", region_name="us-east-1") as client:
            try:
                account_id = await self._get_account_id(client)
                
                # Fetch all budgets
                paginator = client.get_paginator("describe_budgets")
                budgets = []
                
                async for page in paginator.paginate(AccountId=account_id):
                    for budget in page.get("Budgets", []):
                        # Extract spend data
                        limit = budget.get("BudgetLimit", {}).get("Amount", "0")
                        actual = budget.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", "0")
                        forecasted = budget.get("CalculatedSpend", {}).get("ForecastedSpend", {}).get("Amount", "0")
                        
                        budgets.append({
                            "BudgetName": budget.get("BudgetName"),
                            "BudgetType": budget.get("BudgetType"),
                            "Limit": f"{limit} {budget.get('BudgetLimit', {}).get('Unit', 'USD')}",
                            "ActualSpend": f"{actual} USD",
                            "ForecastedSpend": f"{forecasted} USD",
                            "PercentageUsed": f"{(float(actual)/float(limit))*100:.1f}%" if float(limit) > 0 else "N/A"
                        })
                
                return {
                    "AccountID": account_id,
                    "TotalBudgets": len(budgets),
                    "BudgetStatus": budgets
                }
                
            except Exception as e:
                return {"Error": f"Failed to fetch budgets: {str(e)}"}

# ================================================================== #
# Execution and Agent integration                                   #
# ================================================================== #

# async def main():
#     fetcher = AWSBudgetsFetcher()
#     print("\n--- Fetching AWS Budget Status ---")
#     report = await fetcher.fetch_budget_status()
#     print(json.dumps(report, indent=4))

# async def run_agent_observation() -> None:
#     fetcher = AWSBudgetsFetcher()
#     tools = [function_tool(fetcher.fetch_budget_status)]
    
#     agent = Agent(
#         name="Chandra", 
#         instructions="Analyze budget status. Flag any budget where 'PercentageUsed' exceeds 80%.", 
#         model="gpt-5.4-nano", 
#         tools=tools
#     )
    
#     result = await Runner.run(agent, "What is my current budget status? Are any budgets nearing their limits?")
#     print("\n--- Agent Final Output ---")
#     print(result.final_output)

# if __name__ == "__main__":
#     asyncio.run(main())
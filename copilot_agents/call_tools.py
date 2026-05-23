from botocore.exceptions import ClientError
import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Annotated, TypedDict

import boto3
import pytz
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

load_dotenv(override=True)

# ── Clients ──────────────────────────────────────────────────────────────────
cloudwatch_client = boto3.client("cloudwatch")
xray_client = boto3.client("xray")
config_client = boto3.client("config")
cloudtrail_client = boto3.client("cloudtrail")
ce_client = boto3.client("ce", region_name="us-east-1")

# ── Tool 1: CloudWatch CPU Metrics ───────────────────────────────────────────
@tool
def get_cpu_metrics(
    instanceId: str,
    Instance: str = "AWS/EC2",
    Metric: str = "CPUUtilization",
    last_hours: int = 6,
    period: int = 1800,
) -> dict:
    """
    Fetches CPUUtilization metrics for the user-provided instance
    over the last N hours with configurable period intervals.

    Args:
        instanceId: EC2 instance ID (e.g. i-0327bf109dc40d412)
        Instance:   CloudWatch namespace  (default: AWS/EC2)
        Metric:     Metric name           (default: CPUUtilization)
        last_hours: Look-back window in hours  (default: 6)
        period:     Aggregation period in seconds (default: 1800 = 30 min)
    """
    end_time = datetime.now(pytz.utc)
    start_time = end_time - timedelta(hours=last_hours)

    response = cloudwatch_client.get_metric_statistics(
        Namespace=Instance,
        MetricName=Metric,
        Dimensions=[{"Name": "InstanceId", "Value": instanceId}],
        StartTime=start_time,
        EndTime=end_time,
        Period=period,
        Statistics=["Average"],
        Unit="Percent",
    )

    ist = pytz.timezone("Asia/Kolkata")
    for point in response["Datapoints"]:
        point["Timestamp"] = (
            point["Timestamp"].astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")
        )

    response["Datapoints"].sort(key=lambda x: x["Timestamp"], reverse=True)
    return {"Datapoints": response["Datapoints"]}


# ── Tool 2: AWS X-Ray Traces (Modified to Match) ──────────────────────────────
@tool
def get_xray_traces(last_hours: int = 8) -> dict:
    """
    Fetches AWS X-Ray trace summaries for requests moving between services
    over a configurable look-back window in hours.

    Args:
        last_hours: Look-back window in hours (default: 8)
    """
    end_time = datetime.now(pytz.utc)
    start_time = end_time - timedelta(hours=last_hours)

    # Fetch summaries from AWS SDK
    response = xray_client.get_trace_summaries(
        StartTime=start_time, EndTime=end_time
    )

    ist = pytz.timezone("Asia/Kolkata")
    trace_summaries = response.get("TraceSummaries", [])

    # Process and format the trace summaries inline for the LLM
    for trace in trace_summaries:
        # Determine Status string
        if trace.get("HasFault"):
            trace["Status"] = "Fault (5xx)"
        elif trace.get("HasError"):
            trace["Status"] = "Error (4xx)"
        else:
            trace["Status"] = "Success (200)"

        # Convert entry time timestamp to human-readable IST if present
        if "Http" in trace and "HttpURL" in trace["Http"]:
            # Note: Optional print left for console debugging, but returning clean data
            print(
                f"Path: {trace['Http']['HttpURL']} | Duration: {trace.get('Duration')}s | Status: {trace['Status']}"
            )

    # Clean return pattern payload matching standard tool conventions
    return {"TraceSummaries": trace_summaries}

# ── Tool 3: AWS Config Resources (Modified to Match) ─────────────────────────
@tool
def get_recent_resource_changes(limit: int = 20) -> dict:
    """
    Uses AWS Config advanced queries (SQL) to audit and fetch recently modified
    or deleted AWS infrastructure resources.

    Args:
        limit: Max number of history logs to return (default: 20, max: 100)
    """
    # SQL-like query targeting resources that were deleted or updated
    sql_query = """
        SELECT 
            resourceId, 
            resourceType, 
            configurationItemCaptureTime, 
            configurationItemStatus 
        WHERE 
            configurationItemStatus = 'ResourceDeleted' 
            OR configurationItemStatus = 'OK'
        ORDER BY 
            configurationItemCaptureTime DESC
    """

    try:
        response = config_client.select_resource_config(
            Expression=sql_query, Limit=limit
        )

        results = response.get("Results", [])
        parsed_results = []

        # AWS Config returns stringified JSON blocks; parse them cleanly for the LLM
        for result in results:
            parsed_results.append(json.loads(result))

        return {"RecentChanges": parsed_results}

    except ClientError as e:
        # Instead of generic console printing, return the error context back to the agent
        return {"Error": f"AWS Config advanced query execution failed: {str(e)}"}

# ── Tool 4: AWS CloudTrail Audit Logs (Modified to Match) ────────────────────
@tool
def get_cloudtrail_api_logs(hours_lookback: int = 2, max_results: int = 10) -> dict:
    """
    Looks up recent management and deployment operational events, user activities, 
    and AWS API operational history logs using CloudTrail.

    Args:
        hours_lookback: Look-back window in hours (default: 2)
        max_results: Max number of event records to return (default: 10, max: 50)
    """
    end_time = datetime.now(pytz.utc)
    start_time = end_time - timedelta(hours=hours_lookback)
    ist = pytz.timezone("Asia/Kolkata")

    try:
        response = cloudtrail_client.lookup_events(
            StartTime=start_time,
            EndTime=end_time,
            MaxResults=max_results
        )
        
        events = response.get("Events", [])
        processed_events = []

        for event in events:
            # Parse human readable event metadata
            event_time_utc = event.get("EventTime")
            event_time_ist = event_time_utc.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST") if event_time_utc else None
            
            event_entry = {
                "EventId": event.get("EventId"),
                "EventName": event.get("EventName"),
                "Username": event.get("Username"),
                "EventTime": event_time_ist,
                "ParsedPayload": {}
            }

            # De-serialize the nested CloudTrail JSON document block for LLM comprehension
            if "CloudTrailEvent" in event:
                try:
                    raw_payload = json.loads(event["CloudTrailEvent"])
                    event_entry["ParsedPayload"] = {
                        "sourceIPAddress": raw_payload.get("sourceIPAddress"),
                        "userAgent": raw_payload.get("userAgent"),
                        "requestParameters": raw_payload.get("requestParameters"),
                        "responseElements": raw_payload.get("responseElements")
                    }
                except Exception:
                    event_entry["ParsedPayload"] = {"Raw": event["CloudTrailEvent"][:500]}

            processed_events.append(event_entry)

        return {"CloudTrailEvents": processed_events}

    except ClientError as e:
        return {"Error": f"CloudTrail event tracking lookup failed: {str(e)}"}

# ── Tool 5: AWS Cost Explorer Summary (Modified to Match) ────────────────────
@tool
def get_aws_cost_summary(days_lookback: int = 7) -> dict:
    """
    Fetches AWS cost and usage financial metrics broken down and grouped by AWS Service 
    over a configurable daily historical window.

    Args:
        days_lookback: Number of past days to analyze (default: 7)
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_lookback)).strftime("%Y-%m-%d")

    try:
        response = ce_client.get_cost_and_usage(
            TimePeriod={
                "Start": start_date,
                "End": end_date
            },
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[
                {
                    "Type": "DIMENSION",
                    "Key": "SERVICE"
                }
            ]
        )
        
        results_by_time = response.get("ResultsByTime", [])
        cost_summary = []

        for period in results_by_time:
            time_frame = period.get("TimePeriod", {})
            day_entry = {
                "Start": time_frame.get("Start"),
                "End": time_frame.get("End"),
                "ActiveCosts": []
            }
            
            groups = period.get("Groups", [])
            for group in groups:
                service_name = group.get("Keys", ["Unknown"])[0]
                metrics = group.get("Metrics", {})
                unblended_cost = metrics.get("UnblendedCost", {})
                
                amount = float(unblended_cost.get("Amount", 0))
                unit = unblended_cost.get("Unit", "USD")
                
                # Filter out free tier / zero-cost lines to optimize model prompt size
                if amount > 0.001:
                    day_entry["ActiveCosts"].append({
                        "Service": service_name,
                        "Amount": round(amount, 4),
                        "Unit": unit
                    })
            
            cost_summary.append(day_entry)

        return {"DailyCostBreakdown": cost_summary}

    except ClientError as e:
        return {"Error": f"AWS Cost Explorer query failed: {str(e)}"}
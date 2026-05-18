import time
from datetime import datetime, timedelta
import json
import boto3
from dotenv import load_dotenv
import pytz

load_dotenv(override=True)
cloudwatch = boto3.client("cloudwatch")

def cloudwatch(last_hours:int=6, period:int=1800):
    """
    Fetches CPUUtilization metrics for the EC2 instance i-0327bf109dc40d412
    over the last 6 hours with 30-minute intervals.
    """
    end_time = datetime.now(pytz.utc)
    start_time = end_time - timedelta(hours=6)

    response = cloudwatch.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": "i-0327bf109dc40d412"}],
        StartTime=start_time,
        EndTime=end_time,
        Period=1800,
        Statistics=["Average"],
        Unit="Percent",
    )

    ist = pytz.timezone("Asia/Kolkata")
    
    for point in response["Datapoints"]:
        point["Timestamp"] = (
            point["Timestamp"].astimezone(ist).strftime("%Y-%m-%d %H:%M:%S IST")
        )
    
    response["Datapoints"].sort(key=lambda x: x["Timestamp"], reverse=True)

    return response


import os
import json
from datetime import datetime, timedelta
import boto3
from dotenv import load_dotenv
import pytz

load_dotenv(override=True)

# Initialize the X-Ray client
xray = boto3.client("xray")

def get_xray_traces():
    """
    Fetches X-Ray trace summaries for requests moving between services
    over the last 2 hours.
    """
    end_time = datetime.now(pytz.utc)
    start_time = end_time - timedelta(hours=8)

    # Note: X-Ray API requires datetime objects for StartTime and EndTime
    response = xray.get_trace_summaries(
        StartTime=start_time,
        EndTime=end_time
    )

    ist = pytz.timezone("Asia/Kolkata")
    
    # Process and format the trace summaries
    for trace in response.get("TraceSummaries", []):
        # Format the entry time to IST
        if "HasFault" in trace and trace["HasFault"]:
            trace["Status"] = "Fault (5xx)"
        elif "HasError" in trace and trace["HasError"]:
            trace["Status"] = "Error (4xx)"
        else:
            trace["Status"] = "Success (200)"

        # Convert timestamps for readability
        if "Http" in trace and "HttpURL" in trace["Http"]:
            print(f"Path: {trace['Http']['HttpURL']} | Duration: {trace.get('Duration')}s | Status: {trace['Status']}")

    return response


import json
import boto3
# Import ClientError from botocore.exceptions to handle AWS API errors
from botocore.exceptions import ClientError

# Initialize the AWS Config client
config_client = boto3.client("config")

def aws_config():
    """
    Uses AWS Config advanced query to look for recently modified resources.
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
            Expression=sql_query,
            Limit=20
        )
        
        results = response.get("Results", [])
        print(f"\n--- Found {len(results)} recent configuration updates via SQL query ---")
        
        for result in results:
            parsed_result = json.loads(result)
            print(json.dumps(parsed_result, indent=2))
            
    except ClientError as e:
        print(f"SQL Query Failed: {e}")


from datetime import datetime, timedelta
import json
import boto3
from botocore.exceptions import ClientError
import pytz

# Initialize the CloudTrail client
cloudtrail_client = boto3.client("cloudtrail")

def get_cloudtrail_api_logs(hours_lookback=2):
    """
    Looks up recent AWS API activity logs using CloudTrail.
    """
    end_time = datetime.now(pytz.utc)
    start_time = end_time - timedelta(hours=hours_lookback)
    
    print(f"Fetching CloudTrail API logs from {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')} UTC...")

    try:
        # Lookup management events
        response = cloudtrail_client.lookup_events(
            StartTime=start_time,
            EndTime=end_time,
            MaxResults=10  # Adjust as needed (Max: 50 per page)
        )
        
        events = response.get("Events", [])
        print(f"\n--- Found {len(events)} API events ---")
        
        for event in events:
            event_name = event.get("EventName")      # e.g., RunInstances, DeleteSecurityGroup
            username = event.get("Username")          # The IAM entity that made the call
            event_time = event.get("EventTime")
            
            print(f"\n⏱️ Time: {event_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"🚀 Action: {event_name} | 👤 User: {username}")
            
            # The actual raw CloudTrail log payload is a JSON string inside 'CloudTrailEvent'
            if "CloudTrailEvent" in event:
                raw_payload = json.loads(event["CloudTrailEvent"])
                
                # Extracting useful details from the deep payload
                source_ip = raw_payload.get("sourceIPAddress", "Unknown IP")
                user_agent = raw_payload.get("userAgent", "Unknown Agent")
                
                print(f"🌐 Source IP: {source_ip}")
                # Print truncated raw payload for inspection
                print("📄 Raw Event Sample:")
                print(json.dumps(raw_payload, indent=2)[:400] + "... [Truncated]")
                
        return events

    except ClientError as e:
        print(f"CloudTrail Query Failed: {e}")
        return None



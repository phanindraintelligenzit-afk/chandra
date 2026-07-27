import asyncio
import boto3
import json
import logging
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

IMPORTANT_METRICS = {
    "AWS/EC2": [
        "CPUUtilization",
        "NetworkIn",
        "NetworkOut",
        "StatusCheckFailed",
        "EBSReadBytes",
        "EBSWriteBytes",
    ],
    "AWS/EBS": [
        "VolumeReadBytes",
        "VolumeWriteBytes",
        "VolumeReadOps",
        "VolumeWriteOps",
        "VolumeQueueLength",
        "BurstBalance",
    ],
    "AWS/RDS": [
        "CPUUtilization",
        "DatabaseConnections",
        "FreeStorageSpace",
        "FreeableMemory",
        "ReadIOPS",
        "WriteIOPS",
        "ReadLatency",
        "WriteLatency",
        "ReplicaLag",
    ],
    "AWS/Lambda": [
        "Invocations",
        "Errors",
        "Throttles",
        "Duration",
        "ConcurrentExecutions",
        "DeadLetterErrors",
        "IteratorAge",
    ],
    "AWS/SQS": [
        "NumberOfMessagesSent",
        "NumberOfMessagesReceived",
        "NumberOfMessagesDeleted",
        "ApproximateNumberOfMessagesVisible",
        "ApproximateAgeOfOldestMessage",
        "NumberOfEmptyReceives",
    ],
    "AWS/SNS": [
        "NumberOfMessagesPublished",
        "NumberOfNotificationsDelivered",
        "NumberOfNotificationsFailed",
    ],
    "AWS/DynamoDB": [
        "ConsumedReadCapacityUnits",
        "ConsumedWriteCapacityUnits",
        "ReadThrottleEvents",
        "WriteThrottleEvents",
        "SuccessfulRequestLatency",
        "SystemErrors",
    ],
    "AWS/S3": [
        "BucketSizeBytes",
        "NumberOfObjects",
        "4xxErrors",
        "5xxErrors",
        "FirstByteLatency",
        "TotalRequestLatency",
    ],
    "AWS/ECS": [
        "CPUUtilization",
        "MemoryUtilization",
        "RunningTaskCount",
        "PendingTaskCount",
    ],
}


class CloudWatchMetricsFetcher:

    def __init__(self):
        pass

    async def fetch_all_metrics(
        self,
        region: str,
        last_hours: int,
        period: int,
        timezone_str: str,
    ) -> dict:
        target_timezone = pytz.timezone(timezone_str)
        client          = boto3.client("cloudwatch", region_name=region)

        end_time   = datetime.now(pytz.utc)
        start_time = end_time - timedelta(hours=last_hours)

        # --- discover ---
        logger.info("Discovering metrics selectively by namespace...")
        
        all_metrics = []
        for namespace in IMPORTANT_METRICS.keys():
            token = None
            while True:
                kwargs = {"Namespace": namespace}
                if token:
                    kwargs["NextToken"] = token
                resp = await asyncio.to_thread(client.list_metrics, **kwargs)
                all_metrics.extend(resp.get("Metrics", []))
                token = resp.get("NextToken")
                if not token:
                    break
                    
        logger.info("Found %d metrics matching target namespaces.", len(all_metrics))

        # --- filter ---
        filtered = [
            m for m in all_metrics
            if m["MetricName"] in IMPORTANT_METRICS.get(m["Namespace"], [])
        ]
        logger.info(
            "Filtered to %d core metrics across %d namespaces.",
            len(filtered),
            len(set(m["Namespace"] for m in filtered)),
        )

        # --- batch and fetch ---
        batches = [
            filtered[i : i + BATCH_SIZE]
            for i in range(0, len(filtered), BATCH_SIZE)
        ]
        logger.info("Launching %d concurrent batch request(s)...", len(batches))

        async def _fetch_batch(batch: list, batch_offset: int) -> list:
            queries = [
                {
                    "Id": f"avg{batch_offset + local_i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace":  m["Namespace"],
                            "MetricName": m["MetricName"],
                            "Dimensions": m["Dimensions"],
                        },
                        "Period": period,
                        "Stat":   "Average",
                    },
                    "ReturnData": True,
                }
                for local_i, m in enumerate(batch)
            ]
            try:
                resp = await asyncio.to_thread(
                    client.get_metric_data,
                    MetricDataQueries=queries,
                    StartTime=start_time,
                    EndTime=end_time,
                )
                return resp.get("MetricDataResults", [])
            except Exception as e:
                logger.warning("Batch fetch failed: %s", e)
                return []

        tasks, offset = [], 0
        for batch in batches:
            tasks.append(_fetch_batch(batch, offset))
            offset += len(batch)

        batch_results = await asyncio.gather(*tasks)

        # --- parse results ---
        id_to_meta: dict[int, dict] = {i: m for i, m in enumerate(filtered)}
        avg_data:   dict[int, dict] = {}

        for results in batch_results:
            for result in results:
                if not result.get("Values"):
                    continue
                gid = int(result["Id"][3:])   # strip "avg" prefix
                avg_data[gid] = {
                    "timestamps": result["Timestamps"],
                    "values":     result["Values"],
                }

        # --- assemble output ---
        output: dict = {
            "metadata": {
                "region":                region,
                "start_time":            start_time.astimezone(target_timezone).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "end_time":              end_time.astimezone(target_timezone).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "period_seconds":        period,
                "total_metrics_found":   len(all_metrics),
                "total_metrics_fetched": len(filtered),
            },
            "namespaces": {}
        }

        for gid, meta in id_to_meta.items():
            entry = avg_data.get(gid)
            if not entry:
                continue

            namespace   = meta["Namespace"]
            metric_name = meta["MetricName"]
            dimensions  = {d["Name"]: d["Value"] for d in meta["Dimensions"]}

            datapoints = sorted(
                [
                    {
                        "timestamp": ts.astimezone(target_timezone).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "average":   round(v, 6),
                    }
                    for ts, v in zip(entry["timestamps"], entry["values"])
                ],
                key=lambda x: x["timestamp"],
                reverse=True,
            )

            output["namespaces"] \
                  .setdefault(namespace, {}) \
                  .setdefault(metric_name, []) \
                  .append({
                      "metric_name": metric_name,
                      "dimensions":  dimensions,
                      "datapoints":  datapoints,
                  })

        return output


# async def main():
#     fetcher = CloudWatchMetricsFetcher()

#     start_perf = datetime.now()
#     results = await fetcher.fetch_all_metrics(
#         region="us-east-1",
#         last_hours=6,
#         period=1800,
#         timezone_str="Asia/Kolkata",
#     )
#     end_perf = datetime.now()

#     results["metadata"]["execution_time_seconds"] = round(
#         (end_perf - start_perf).total_seconds(), 3
#     )

#     print(json.dumps(results, indent=2))


# if __name__ == "__main__":
#     asyncio.run(main())
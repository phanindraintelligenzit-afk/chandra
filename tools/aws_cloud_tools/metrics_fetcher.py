import asyncio
import aioboto3
import pytz
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv(override=True)

class CloudWatchMetricsFetcher:
    """
    Asynchronously fetches CloudWatch metrics across all (or specific) namespaces.

    Report structure:
        {namespace: {dimension_key: {metric_name: [datapoints]}}}

    Requires: pip install aioboto3 pytz
    """

    def __init__(
        self,
        timezone: str = "Asia/Kolkata",
        max_concurrent: int = 20,
        metric_data_batch_size: int = 200,
    ):
        self._tz = pytz.timezone(timezone)
        self._max_concurrent = max_concurrent
        self.__semaphore = None
        self._session = aioboto3.Session()
        # CloudWatch allows up to 500 queries per GetMetricData call.
        self._metric_data_batch_size = max(1, min(metric_data_batch_size, 500))

    @property
    def _semaphore(self) -> asyncio.Semaphore:
        if self.__semaphore is None:
            self.__semaphore = asyncio.Semaphore(self._max_concurrent)
        return self.__semaphore

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _list_metrics(
        self, cloudwatch, namespace: Optional[str] = None
    ) -> list[dict]:
        kwargs = {"Namespace": namespace} if namespace else {}
        metrics: list[dict] = []
        paginator = cloudwatch.get_paginator("list_metrics")
        async for page in paginator.paginate(**kwargs):
            metrics.extend(page.get("Metrics", []))
        return metrics

    async def _list_regions(self) -> list[str]:
        """Return all enabled AWS regions for the current account."""
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            response = await ec2.describe_regions(AllRegions=False)
        return sorted(region["RegionName"] for region in response.get("Regions", []))

    async def _get_statistics(
        self,
        cloudwatch,
        namespace: str,
        metric_name: str,
        dimensions: list[dict],
        start_time: datetime,
        end_time: datetime,
        period: int,
    ) -> list[dict]:
        async with self._semaphore:
            response = await cloudwatch.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start_time,
                EndTime=end_time,
                Period=period,
                Statistics=["Average"],
            )

        datapoints = response.get("Datapoints", [])
        for pt in datapoints:
            pt["Timestamp"] = (
                pt["Timestamp"].astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z")
            )
        datapoints.sort(key=lambda x: x["Timestamp"], reverse=True)
        return datapoints

    async def _get_metric_data_batch(
        self,
        cloudwatch,
        metrics_batch: list[dict],
        start_time: datetime,
        end_time: datetime,
        period: int,
    ) -> dict[str, list[dict]]:
        """
        Fetch a batch of metrics in one API call using GetMetricData.

        Returns:
            {query_id: [{"Timestamp": str, "Average": float}, ...]}
        """
        id_to_query: dict[str, dict] = {}
        metric_data_queries: list[dict] = []

        for idx, metric in enumerate(metrics_batch):
            query_id = f"m{idx}"
            id_to_query[query_id] = metric
            metric_data_queries.append(
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": metric["Namespace"],
                            "MetricName": metric["MetricName"],
                            "Dimensions": metric["Dimensions"],
                        },
                        "Period": period,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            )

        next_token = None
        results: dict[str, list[dict]] = {query_id: [] for query_id in id_to_query}
        while True:
            request_kwargs = {
                "StartTime": start_time,
                "EndTime": end_time,
                "MetricDataQueries": metric_data_queries,
            }
            if next_token:
                request_kwargs["NextToken"] = next_token

            async with self._semaphore:
                response = await cloudwatch.get_metric_data(**request_kwargs)

            for metric_result in response.get("MetricDataResults", []):
                query_id = metric_result["Id"]
                timestamps = metric_result.get("Timestamps", [])
                values = metric_result.get("Values", [])
                points = [
                    {
                        "Timestamp": ts.astimezone(self._tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
                        "Average": value,
                    }
                    for ts, value in zip(timestamps, values, strict=False)
                ]
                results[query_id].extend(points)

            next_token = response.get("NextToken")
            if not next_token:
                break

        for query_id in results:
            results[query_id].sort(key=lambda x: x["Timestamp"], reverse=True)

        return results

    @staticmethod
    def _dim_key(dimensions: list[dict]) -> str:
        return (
            ", ".join(
                f"{d['Name']}={d['Value']}"
                for d in sorted(dimensions, key=lambda d: d["Name"])
            )
            or "<no dimensions>"
        )

    @staticmethod
    def _chunk(items: list[dict], size: int) -> list[list[dict]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    @staticmethod
    def _dedupe_metrics(metrics: list[dict]) -> list[dict]:
        seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
        deduped: list[dict] = []
        for metric in metrics:
            dims = tuple(
                (d["Name"], d["Value"])
                for d in sorted(metric.get("Dimensions", []), key=lambda d: d["Name"])
            )
            key = (metric["Namespace"], metric["MetricName"], dims)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(metric)
        return deduped

    async def _collect(
        self,
        cloudwatch,
        metric: dict,
        start_time: datetime,
        end_time: datetime,
        period: int,
        report: dict,
    ) -> None:
        namespace = metric["Namespace"]
        metric_name = metric["MetricName"]
        dimensions = metric["Dimensions"]

        try:
            datapoints = await self._get_statistics(
                cloudwatch, namespace, metric_name, dimensions,
                start_time, end_time, period,
            )
        except Exception:
            return

        if datapoints:
            dim_key = self._dim_key(dimensions)
            report.setdefault(namespace, {}).setdefault(dim_key, {})[metric_name] = datapoints

    async def _collect_batch_with_metric_data(
        self,
        cloudwatch,
        metrics_batch: list[dict],
        start_time: datetime,
        end_time: datetime,
        period: int,
        report: dict,
    ) -> None:
        try:
            batch_results = await self._get_metric_data_batch(
                cloudwatch, metrics_batch, start_time, end_time, period
            )
        except Exception:
            return

        for idx, metric in enumerate(metrics_batch):
            query_id = f"m{idx}"
            datapoints = batch_results.get(query_id, [])
            if not datapoints:
                continue

            namespace = metric["Namespace"]
            metric_name = metric["MetricName"]
            dim_key = self._dim_key(metric["Dimensions"])
            report.setdefault(namespace, {}).setdefault(dim_key, {})[metric_name] = datapoints

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def fetch_all_metrics(
        self,
        region: str,
        last_hours: int = 20,
        period: int = 1800,
        use_metric_data: bool = True,
    ) -> dict:
        """
        Fetch metrics from every namespace active in the last 2 weeks.

        CloudWatch's list_metrics only returns metrics with recent data,
        so this naturally covers all live namespaces without a separate
        namespace discovery step.
        """
        end_time = datetime.now(pytz.utc)
        start_time = end_time - timedelta(hours=last_hours)
        report: dict = {}

        async with self._session.client("cloudwatch", region_name=region) as cw:
            metrics = await self._list_metrics(cw)
            metrics = self._dedupe_metrics(metrics)
            if use_metric_data:
                batches = self._chunk(metrics, self._metric_data_batch_size)
                await asyncio.gather(
                    *[
                        self._collect_batch_with_metric_data(
                            cw, batch, start_time, end_time, period, report
                        )
                        for batch in batches
                    ],
                    return_exceptions=True,
                )
            else:
                await asyncio.gather(
                    *[
                        self._collect(cw, m, start_time, end_time, period, report)
                        for m in metrics
                    ],
                    return_exceptions=True,
                )

        return report

    async def fetch_namespace_metrics(
        self,
        region: str,
        namespace: str,
        last_hours: int = 20,
        period: int = 1800,
        use_metric_data: bool = True,
    ) -> dict:
        """
        Fetch metrics for a single namespace.

        Returns: {dimension_key: {metric_name: [datapoints]}}
        """
        end_time = datetime.now(pytz.utc)
        start_time = end_time - timedelta(hours=last_hours)
        report: dict = {}

        async with self._session.client("cloudwatch", region_name=region) as cw:
            metrics = await self._list_metrics(cw, namespace=namespace)
            metrics = self._dedupe_metrics(metrics)
            if use_metric_data:
                batches = self._chunk(metrics, self._metric_data_batch_size)
                await asyncio.gather(
                    *[
                        self._collect_batch_with_metric_data(
                            cw, batch, start_time, end_time, period, report
                        )
                        for batch in batches
                    ],
                    return_exceptions=True,
                )
            else:
                await asyncio.gather(
                    *[
                        self._collect(cw, m, start_time, end_time, period, report)
                        for m in metrics
                    ],
                    return_exceptions=True,
                )

        return report.get(namespace, {})

    async def fetch_all_regions_metrics(
        self,
        last_hours: int = 20,
        period: int = 1800,
        use_metric_data: bool = True,
        regions: Optional[list[str]] = None,
    ) -> dict[str, dict]:
        """
        Fetch metrics from all enabled regions.

        Returns:
            {region: {namespace: {dimension_key: {metric_name: [datapoints]}}}}
        """
        target_regions = regions or await self._list_regions()
        region_reports = await asyncio.gather(
            *[
                self.fetch_all_metrics(
                    region=region,
                    last_hours=last_hours,
                    period=period,
                    use_metric_data=use_metric_data,
                )
                for region in target_regions
            ],
            return_exceptions=True,
        )

        combined: dict[str, dict] = {}
        for region, result in zip(target_regions, region_reports, strict=False):
            if isinstance(result, Exception):
                combined[region] = {"_error": str(result)}
                continue
            combined[region] = result
        return combined

    async def fetch_metrics_summary(
        self,
        region: str,
        last_hours: int = 6,
        period: int = 1800,
        namespace: Optional[str] = None,
        max_dimension_groups: int = 10,
        max_metrics_per_group: int = 8,
        latest_points: int = 2,
    ) -> dict:
        """
        Fetch a compact summary suitable for LLM tool output.

        Returns a trimmed structure with only top dimension groups and metrics,
        and only the latest N datapoints per metric.
        """
        if namespace:
            raw_report = await self.fetch_namespace_metrics(
                region=region,
                namespace=namespace,
                last_hours=last_hours,
                period=period,
                use_metric_data=True,
            )
            report_by_namespace = {namespace: raw_report}
        else:
            report_by_namespace = await self.fetch_all_metrics(
                region=region,
                last_hours=last_hours,
                period=period,
                use_metric_data=True,
            )

        summary: dict = {
            "region": region,
            "last_hours": last_hours,
            "period_seconds": period,
            "namespace_filter": namespace,
            "namespaces": {},
        }

        for ns, dim_groups in report_by_namespace.items():
            if not isinstance(dim_groups, dict):
                continue
            compact_groups: dict = {}
            for dim_key, metrics_map in list(dim_groups.items())[:max_dimension_groups]:
                if not isinstance(metrics_map, dict):
                    continue
                compact_metrics: dict = {}
                for metric_name, datapoints in list(metrics_map.items())[:max_metrics_per_group]:
                    if not isinstance(datapoints, list) or not datapoints:
                        continue
                    trimmed_points = datapoints[:latest_points]
                    averages = [
                        p.get("Average")
                        for p in datapoints
                        if isinstance(p, dict) and isinstance(p.get("Average"), (int, float))
                    ]
                    metric_summary = {
                        "points": trimmed_points,
                        "sample_count": len(datapoints),
                    }
                    if averages:
                        metric_summary["avg_of_average"] = round(sum(averages) / len(averages), 4)
                        metric_summary["max_average"] = round(max(averages), 4)
                    compact_metrics[metric_name] = metric_summary
                if compact_metrics:
                    compact_groups[dim_key] = compact_metrics
            if compact_groups:
                summary["namespaces"][ns] = compact_groups

        summary["namespace_count"] = len(summary["namespaces"])
        return summary

    async def list_namespaces(self, region: str) -> list[str]:
        """List all namespaces that have reported metrics in the last 2 weeks."""
        async with self._session.client("cloudwatch", region_name=region) as cw:
            metrics = await self._list_metrics(cw)
        return sorted({m["Namespace"] for m in metrics})


# async def main():
#     fetcher = CloudWatchMetricsFetcher()
#     report = await fetcher.fetch_metrics_summary(region="us-east-1", last_hours=4, period=1800)
#     output_path = Path("observation") / "metrics.json"
#     output_path.parent.mkdir(parents=True, exist_ok=True)
#     output_path.write_text(json.dumps(report, indent=4), encoding="utf-8")
#     print(json.dumps(report, indent=4))



# from agents import Agent, Runner, trace, function_tool


# async def run_agent_observation() -> None:
#     fetcher = CloudWatchMetricsFetcher()
#     tools = [function_tool(fetcher.fetch_metrics_summary)]
#     prompt = "You are a cloud watcher agent that monitors cloud infrastructure."
#     agent = Agent(name="Chandra", instructions=prompt, model="gpt-5.4-nano", tools=tools)
#     result = await Runner.run(
#         agent,
#         "Give observations from the cloud watch metrics"
#     )
#     print(result.final_output)


# if __name__ == "__main__":
#     asyncio.run(run_agent_observation())

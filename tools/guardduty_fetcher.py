import asyncio
import aioboto3
import json
from datetime import datetime, timezone
from typing import Any, Optional

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv

load_dotenv()


def _json_default(obj: Any) -> str:
    """Serialize boto3 types (datetime, Decimal, etc.) for json.dump."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class AWSGuardDutyFetcher:
    """
    GuardDuty with bounded output (similar idea to ``fetch_metrics_summary``
    in ``tools/metrics_fetcher.py``): caps per region / total and returns slim rows only.
    """

    # 0 = same as original tool1 (non-archived only). Use 4.0+ to drop low/informational.
    DEFAULT_MIN_SEVERITY = 0.0
    DEFAULT_MAX_PER_REGION = 12
    DEFAULT_MAX_TOTAL = 35

    def __init__(self, max_concurrent: int = 8):
        self._session = aioboto3.Session()
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def _list_regions(self) -> list[str]:
        async with self._session.client("ec2", region_name="us-east-1") as ec2:
            regions = await ec2.describe_regions(AllRegions=False)
        return sorted(r["RegionName"] for r in regions.get("Regions", []))

    @staticmethod
    def _parse_finding(raw: dict, region: str) -> dict[str, Any]:
        """Drop nested Service/RuntimeDetails blobs; keep fields useful for triage."""
        resource = raw.get("Resource") or {}
        created = raw.get("CreatedAt")
        updated = raw.get("UpdatedAt")
        return {
            "Region": region,
            "Id": raw.get("Id"),
            "Type": raw.get("Type"),
            "Severity": raw.get("Severity"),
            "Title": raw.get("Title"),
            "Description": (raw.get("Description") or "")[:320],
            "ResourceType": resource.get("ResourceType"),
            "ResourceId": (resource.get("Id") or resource.get("InstanceId") or "")[:240],
            "Count": (raw.get("Service") or {}).get("Count"),
            "CreatedAt": created.isoformat() if isinstance(created, datetime) else created,
            "UpdatedAt": updated.isoformat() if isinstance(updated, datetime) else updated,
        }

    @staticmethod
    def _finding_criteria(min_severity: float) -> dict[str, Any]:
        """Match original list filter; optional severity floor when min_severity > 0."""
        criterion: dict[str, Any] = {
            "service.archived": {"Eq": ["false"]},
        }
        if min_severity > 0:
            criterion["severity"] = {"Gte": min_severity}
        return {"Criterion": criterion}

    async def _fetch_region_slim(
        self,
        region: str,
        max_ids: int,
        min_severity: float,
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        """
        Returns (slim findings, error_message).
        error_message is set when the API call fails (previously swallowed as empty).
        """
        cap = min(max(1, max_ids), 50)  # GuardDuty API limit per list_findings call
        async with self._semaphore:
            try:
                async with self._session.client("guardduty", region_name=region) as gd:
                    detectors = await gd.list_detectors()
                    d_ids = detectors.get("DetectorIds", [])
                    if not d_ids:
                        return [], None

                    list_resp = await gd.list_findings(
                        DetectorId=d_ids[0],
                        FindingCriteria=self._finding_criteria(min_severity),
                        MaxResults=cap,
                    )
                    ids = list_resp.get("FindingIds", [])
                    if not ids:
                        return [], None

                    detail = await gd.get_findings(
                        DetectorId=d_ids[0],
                        FindingIds=ids[:cap],
                    )
                    return [
                        self._parse_finding(f, region)
                        for f in detail.get("Findings", [])
                    ], None
            except Exception as exc:
                return [], str(exc)

    async def fetch_guardduty_summary(
        self,
        min_severity: float = DEFAULT_MIN_SEVERITY,
        max_findings_per_region: int = DEFAULT_MAX_PER_REGION,
        max_total_findings: int = DEFAULT_MAX_TOTAL,
        regions: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Compact, LLM-friendly summary: counts + top findings only (no full API blobs).
        """
        target = regions or await self._list_regions()
        per_region = await asyncio.gather(
            *[
                self._fetch_region_slim(r, max_findings_per_region, min_severity)
                for r in target
            ],
            return_exceptions=True,
        )

        slim: list[dict[str, Any]] = []
        per_region_counts: dict[str, int] = {}
        region_errors: dict[str, str] = {}
        for region, result in zip(target, per_region, strict=False):
            if isinstance(result, Exception):
                per_region_counts[region] = 0
                region_errors[region] = str(result)
                continue
            findings, err = result
            per_region_counts[region] = len(findings)
            if err:
                region_errors[region] = err
            slim.extend(findings)

        total_before_global_cap = len(slim)
        slim.sort(key=lambda x: float(x.get("Severity") or 0), reverse=True)
        top = slim[:max_total_findings] if slim else []

        high = sum(1 for f in top if float(f.get("Severity") or 0) >= 7.0)
        medium = sum(1 for f in top if 4.0 <= float(f.get("Severity") or 0) < 7.0)
        low = sum(1 for f in top if float(f.get("Severity") or 0) < 4.0)

        by_type: dict[str, int] = {}
        for f in top:
            t = f.get("Type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1

        regions_with_data = [r for r, c in per_region_counts.items() if c > 0]

        summary: dict[str, Any] = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "min_severity_filter": min_severity if min_severity > 0 else None,
            "limits": {
                "max_findings_per_region": max_findings_per_region,
                "max_total_findings_in_report": max_total_findings,
            },
            "regions_scanned": len(target),
            "total_active_threats_fetched": total_before_global_cap,
            "total_active_threats_in_report": len(top),
            "truncated_for_report": total_before_global_cap > len(top),
            "regions_with_active_findings": regions_with_data,
            "per_region_finding_count_capped": per_region_counts,
            "summary_counts_in_top_list": {
                "high_severity_7_plus": high,
                "medium_severity_4_to_6_9": medium,
                "low_severity_below_4": low,
                "rows_in_top_findings": len(top),
            },
            "finding_types_in_top_list": dict(
                sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)[:15]
            ),
            "top_findings": top,
        }
        if region_errors:
            summary["region_errors"] = region_errors
        return summary

    async def fetch_active_threats(self) -> dict[str, Any]:
        """Alias for agents / backward compatibility — returns bounded summary only."""
        return await self.fetch_guardduty_summary()


# async def main() -> None:
#     """Run scan and write a small JSON file (summary, not full GuardDuty payloads)."""
#     print("\n--- Starting Global Security Audit (bounded summary) ---")

#     guardduty = AWSGuardDutyFetcher()
#     report = {
#         "Timestamp": datetime.now(timezone.utc).isoformat(),
#         "GuardDutySummary": await guardduty.fetch_guardduty_summary(),
#     }

#     output_file = "audit_results.json"
#     with open(output_file, "w", encoding="utf-8") as f:
#         json.dump(report, f, indent=2, default=_json_default)

#     inner = report["GuardDutySummary"]
#     print(f"✅ Audit complete. Results saved to {output_file}")
#     print(
#         f"   Active threats fetched: {inner.get('total_active_threats_fetched', 0)} "
#         f"(in report: {inner['total_active_threats_in_report']}) "
#         f"| high≥7: {inner['summary_counts_in_top_list']['high_severity_7_plus']} "
#         f"| regions with data: {len(inner['regions_with_active_findings'])}"
#     )
#     if inner.get("truncated_for_report"):
#         print("   Note: report shows top findings by severity; raise max_total_findings for more.")


# async def run_agent_observation() -> None:
#     guardduty = AWSGuardDutyFetcher()
#     tools = [function_tool(guardduty.fetch_active_threats)]

#     agent = Agent(
#         name="Chandra",
#         instructions=(
#             "You are a security SRE. The tool returns a capped GuardDuty summary only "
#             "(top findings + counts). Triage by severity and type; note that the list may be truncated."
#         ),
#         model="gpt-5.4-nano",
#         tools=tools,
#     )

#     result = await Runner.run(
#         agent,
#         "Summarize active GuardDuty threats from the summary (severity, types, regions).",
#     )
#     print(result.final_output)


# if __name__ == "__main__":
#     asyncio.run(main())
#     asyncio.run(run_agent_observation())

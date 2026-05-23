import asyncio
import importlib
import os
import time
from dotenv import load_dotenv
load_dotenv()

import boto3
from tools.observability_tools import DetectorContext


class ClientFactory:
    def client(self, service_name, region=None):
        kwargs = {"region_name": region} if region else {}
        return boto3.client(service_name, **kwargs)


_MODULES = {
    "compliance": "tools.observability_tools.compliance",
    "security": "tools.observability_tools.security",
    "reliability": "tools.observability_tools.reliability",
    "performance": "tools.observability_tools.performance",
    "cost": "tools.observability_tools.cost",
}


async def _run_one(name: str, account_id: str, regions: list) -> tuple[str, list]:
    t0 = time.perf_counter()
    print(f"Running {name.upper()} detectors...")
    try:
        ctx = DetectorContext(
            account_id=account_id,
            regions=regions,
            factory=ClientFactory(),
        )
        mod = importlib.import_module(_MODULES[name])
        detectors = getattr(mod, "ALL_DETECTORS", None)

        if detectors:
            results = await asyncio.gather(
                *[asyncio.to_thread(fn, ctx) for fn in detectors],
                return_exceptions=True,
            )
            findings = []
            for fn, result in zip(detectors, results):
                if isinstance(result, Exception):
                    print(f"  [{name.upper()}] {fn.__name__} error: {result}")
                else:
                    findings.extend(result)
        else:
            findings = await asyncio.to_thread(mod.run_all, ctx)

        elapsed = time.perf_counter() - t0
        print(f"  Found {len(findings)} {name} issues  ({elapsed:.1f}s)\n")
        return name, findings
    except Exception as e:
        print(f"  Skipped {name} (not found or error): {e}\n")
        return name, []


def _fetch_enabled_regions() -> list[str]:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    response = ec2.describe_regions(AllRegions=False)
    return sorted(r["RegionName"] for r in response.get("Regions", []))


async def run_all_detectors() -> list:
    account_id = os.getenv("SYNTHETIC_ACCOUNT_ID")
    regions = await asyncio.to_thread(_fetch_enabled_regions)

    results = await asyncio.gather(
        *[_run_one(name, account_id, regions) for name in _MODULES]
    )

    return [finding for _, findings in results for finding in findings]


# def main():
#     findings = asyncio.run(run_all_detectors())
#     print(findings)


# if __name__ == "__main__":
#     main()
#     print("\n=== SCRIPT FINISHED SUCCESSFULLY ===")

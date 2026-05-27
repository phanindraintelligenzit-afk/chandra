#!/usr/bin/env python3
"""Ultra Simple + Full Compliance Runner (All KRAs) - Asyncio Optimized with JSON Export"""

import os
import sys
import json
import asyncio
from pathlib import Path
from dataclasses import asdict
from collections import defaultdict
from uuid import uuid4
from src.chandra.briefing.schemas import Finding
from src.chandra.logging import get_logger
from src.chandra.tools.base import DetectorContext, detector_guard, paginate
from src.chandra.aws.client_factory import AwsClientFactory

logger = get_logger(__name__)

logger.info("Script started")
from dotenv import load_dotenv
load_dotenv()
logger.info("Env loaded")

import boto3

# ==================== ADD PROJECT ROOT TO PATH ====================
project_root = str(Path(__file__).parent.resolve())
if project_root not in sys.path:
    sys.path.insert(0, project_root)


# ==================== ASYNC MODULE RUNNER ====================
async def run_module(module_name: str, ctx: DetectorContext) -> list:
    """Imports and runs a single module's run_all function in a background thread."""
    logger.info("Starting %s detectors...", module_name.upper())
    try:
        # 1. Dynamically import the required module
        if module_name == "compliance":
            from src.chandra.tools.compliance import run_all
        elif module_name == "cost":
            from src.chandra.tools.cost import run_all
        elif module_name == "performance":
            from src.chandra.tools.performance import run_all
        elif module_name == "reliability":
            from src.chandra.tools.reliability import run_all
        elif module_name == "security":
            from src.chandra.tools.security import run_all
        else:
            return []

        # 2. Run the blocking, synchronous run_all function in a separate thread
        findings = await asyncio.to_thread(run_all, ctx)
        logger.info("Finished %s: Found %d issues", module_name.upper(), len(findings))
        return findings

    except Exception as e:
        logger.exception("Skipped %s (not found or error)", module_name.upper())
        return []


async def run_all_detectors_async(ctx: DetectorContext) -> list:
    """Creates concurrent tasks for all modules and gathers the results."""
    modules = ["compliance", "security", "reliability", "performance", "cost"]
    
    # Create a list of concurrent tasks
    tasks = [run_module(mod, ctx) for mod in modules]
    
    # Wait for all tasks to complete in parallel
    results = await asyncio.gather(*tasks)
    
    # Flatten the list of lists into a single list of findings
    all_findings = [finding for module_findings in results for finding in module_findings]
    return all_findings


# ==================== MAIN ====================
async def run_all_detectors():
    account_id = os.getenv("SYNTHETIC_ACCOUNT_ID")
    if not account_id:
        logger.error("SYNTHETIC_ACCOUNT_ID not found in .env")
        sys.exit(1)

    logger.info("Using Account ID: %s", account_id)

    ctx = DetectorContext(
        run_id=str(uuid4()),
        account_id=account_id,
        regions=[os.getenv("AWS_DEFAULT_REGION")],          # Add more regions if needed
        factory=AwsClientFactory(fast=True)
    )

    # FIX: Await the coroutine directly instead of trying to start a new event loop
    findings = await run_all_detectors_async(ctx)

    logger.info("Total findings across all KRAs: %d", len(findings))

    # --- ROBUST JSON EXPORT LOGIC ---
    if findings:
        grouped_findings = defaultdict(list)
        for finding in findings:
            # Safely serialize regardless of whether it's Pydantic, a Dataclass, or a Dict
            if hasattr(finding, "model_dump"):        # Pydantic v2
                finding_dict = finding.model_dump()
            elif hasattr(finding, "dict"):            # Pydantic v1
                finding_dict = finding.dict()
            elif hasattr(finding, "__dataclass_fields__"): # Standard python dataclass
                finding_dict = asdict(finding)
            elif isinstance(finding, dict):           # Already a dictionary
                finding_dict = finding
            else:                                     # Standard python object
                finding_dict = vars(finding)
                
            # Safely get the KRA name for grouping
            kra_key = finding_dict.get("kra", "unknown") if isinstance(finding_dict, dict) else getattr(finding, "kra", "unknown")
            
            # --- NEW: Filter out the evidence field ---
            finding_dict.pop("evidence", None)
            
            grouped_findings[kra_key].append(finding_dict)

        logger.info("Successfully grouped findings by KRA")
        return grouped_findings
    else:
        logger.info("No findings generated to save.")
        return {}

# if __name__ == "__main__":
#     asyncio.run(run_all_detectors())
#     logger.info("Script finished successfully")
    
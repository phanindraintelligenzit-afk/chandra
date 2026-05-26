#!/usr/bin/env python3
"""Ultra Simple + Full Compliance Runner (All KRAs)"""
 
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from collections import defaultdict
 
print("=== SCRIPT STARTED ===\n")
 
# Install dependencies if missing
try:
    from dotenv import load_dotenv
except ImportError:
    print("Installing missing dependencies...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dotenv", "boto3"])
    from dotenv import load_dotenv
 
load_dotenv()
print("✅ .env loaded\n")
 
import boto3
 
# ==================== ADD PROJECT ROOT TO PATH ====================
project_root = str(Path(__file__).parent.resolve())
if project_root not in sys.path:
    sys.path.insert(0, project_root)
 
# ==================== LOCAL CLASSES ====================
@dataclass
class Finding:
    """Compatible Finding class"""
    kra: str
    severity: str
    resource_arn: str
    resource_type: str
    region: str
    title: str
    evidence: dict
    recommendation: str
    detector_id: str
 
 
class ClientFactory:
    def client(self, service_name, region=None):
        kwargs = {"region_name": region} if region else {}
        return boto3.client(service_name, **kwargs)
 
 
@dataclass
class DetectorContext:
    account_id: str
    regions: list
    factory: ClientFactory
 
 
# ==================== IMPORT & RUN ALL DETECTORS ====================
def run_all_detectors(ctx: DetectorContext):
    all_findings = []
    # Added "compliance" as requested
    modules = ["compliance", "security", "reliability", "performance", "cost"]
   
    for module_name in modules:
        try:
            print(f"🔄 Running {module_name.upper()} detectors...")
           
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
 
            findings = run_all(ctx)
            all_findings.extend(findings)
            print(f"   ✅ Found {len(findings)} {module_name} issues\n")
           
        except Exception as e:
            print(f"   ⚠️  Skipped {module_name} (not found or error): {e}\n")
 
    return all_findings
 
 
# ==================== MAIN ====================
def main():
    account_id = os.getenv("SYNTHETIC_ACCOUNT_ID")
    if not account_id:
        print("❌ ERROR: SYNTHETIC_ACCOUNT_ID not found in .env")
        sys.exit(1)
 
    print(f"🔑 Using Account ID: {account_id}\n")
 
    ctx = DetectorContext(
        account_id=account_id,
        regions=["us-east-1"],          # Add more regions if needed
        factory=ClientFactory()
    )
 
    findings = run_all_detectors(ctx)
 
    print("=" * 110)
    print(f"🎯 TOTAL FINDINGS ACROSS ALL KRAs: {len(findings)}")
    print("=" * 110)

    print(findings)
 
if __name__ == "__main__":
    main()
    print("\n=== SCRIPT FINISHED SUCCESSFULLY ===")
from src.chandra.dpi import score_worker, DPIDimension

result = score_worker(
    DPIDimension(
        productivity=90,
        quality=88,
        execution=82,
        governance=95,
        risk=90,
        validation=87,
        cost=80,
    ),
    worker_id="test-001",
    worker_name="Arjun",
    worker_role="AWS Cloud Engineer",
    maturity_level="L3",
)

print("=" * 50)
print("  DPI-LS ENGINE TEST")
print("=" * 50)
print(f"  Worker      : {result.worker_name}")
print(f"  Role        : {result.worker_role}")
print(f"  Maturity    : {result.maturity_level}")
print("-" * 50)
print(f"  P Productivity   : {result.dimensions.productivity}")
print(f"  Q Quality        : {result.dimensions.quality}")
print(f"  E Execution      : {result.dimensions.execution}")
print(f"  G Governance     : {result.dimensions.governance}")
print(f"  R Risk           : {result.dimensions.risk}")
print(f"  V Validation     : {result.dimensions.validation}")
print(f"  C Cost           : {result.dimensions.cost}")
print("-" * 50)
print(f"  OVERALL SCORE    : {result.overall_score} / 100")
print(f"  RATING           : {result.rating}")
print(f"  STRENGTHS        : {result.strengths}")
print(f"  IMPROVEMENTS     : {result.improvement_areas}")
print(f"  RECOMMENDATION   : {result.recommendation}")
print("=" * 50)
print("  DPI ENGINE: PASS")
print("=" * 50)

$body = @{
    worker_id = "demo-001"
    worker_name = "Arjun - AWS Cloud Engineer"
    worker_role = "AWS Cloud Engineer"
    maturity_level = "L3"
    dimensions = @{
        productivity = 90
        quality = 88
        execution = 82
        governance = 95
        risk = 90
        validation = 87
        cost = 80
    }
} | ConvertTo-Json -Depth 5

$response = Invoke-RestMethod -Uri "http://localhost:8000/api/dpi/score" -Method POST -ContentType "application/json" -Body $body

Write-Host "=" * 55
Write-Host "  DPI-LS API LIVE TEST"
Write-Host "=" * 55
Write-Host "  worker_name   : $($response.worker_name)"
Write-Host "  worker_role   : $($response.worker_role)"
Write-Host "  maturity      : $($response.maturity_level)"
Write-Host "-" * 55
Write-Host "  P Productivity: $($response.dimensions.productivity)"
Write-Host "  Q Quality     : $($response.dimensions.quality)"
Write-Host "  E Execution   : $($response.dimensions.execution)"
Write-Host "  G Governance  : $($response.dimensions.governance)"
Write-Host "  R Risk        : $($response.dimensions.risk)"
Write-Host "  V Validation  : $($response.dimensions.validation)"
Write-Host "  C Cost        : $($response.dimensions.cost)"
Write-Host "-" * 55
Write-Host "  OVERALL SCORE : $($response.overall_score) / 100"
Write-Host "  RATING        : $($response.rating)"
Write-Host "  RECOMMENDATION: $($response.recommendation)"
Write-Host "=" * 55
Write-Host "  API STATUS    : PASS - 200 OK"
Write-Host "=" * 55

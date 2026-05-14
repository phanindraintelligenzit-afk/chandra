# Chandra demo smoke script (PowerShell, Windows-friendly).
# Mirrors scripts/smoke.sh. Idempotent. Throws on any step failure.

[CmdletBinding()]
param(
    [switch] $SkipTerraform
)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path "$PSScriptRoot\..\"
Set-Location $root

if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([^#=][^=]*)=(.*)$') {
            $env:($Matches[1].Trim()) = $Matches[2].Trim()
        }
    }
}

if (-not $env:SYNTHETIC_ACCOUNT_ID) {
    throw 'SYNTHETIC_ACCOUNT_ID is required (set it in .env).'
}

function Step($msg) {
    Write-Host ""
    Write-Host "▶ $msg" -ForegroundColor Cyan
}

function Ok($msg) {
    Write-Host "✓ $msg" -ForegroundColor Green
}

Step 'Verifying prerequisites'
foreach ($bin in 'uv', 'docker', 'terraform', 'aws') {
    if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) {
        throw "Missing required tool: $bin"
    }
}
Ok 'uv, docker, terraform, aws CLI present'

Step 'Confirming caller identity'
aws sts get-caller-identity --output table

Step 'Bringing up local Postgres'
docker compose up -d postgres
for ($i = 0; $i -lt 30; $i++) {
    $state = docker compose ps postgres 2>&1
    if ($state -match 'healthy') { break }
    Start-Sleep -Seconds 1
}
Ok 'postgres healthy'

Step 'Installing Python deps (uv sync)'
uv sync --all-extras
Ok 'deps installed'

Step 'Running alembic migrations'
uv run alembic upgrade head
Ok 'schema up to date'

if (-not $SkipTerraform) {
    Step 'Applying synthetic Terraform env (5-8 minutes typical)'
    Push-Location iac\synthetic_env
    try {
        terraform init -upgrade -input=false
        terraform apply -auto-approve -input=false
    } finally {
        Pop-Location
    }
    Ok 'synthetic env applied'
} else {
    Step 'Skipping terraform apply (per -SkipTerraform)'
}

Step 'Running Chandra (full observation pipeline)'
$env:CHANDRA_STALE_KEY_DAYS_OVERRIDE = '0'
uv run chandra run --account $env:SYNTHETIC_ACCOUNT_ID
Ok 'run complete — artifacts in evals/reports/'

Step 'Running eval harness'
uv run chandra eval --account $env:SYNTHETIC_ACCOUNT_ID
if ($LASTEXITCODE -ne 0) {
    throw 'Eval thresholds breached — see evals/reports/*.md.'
}
Ok 'eval passed (recall ≥ 80%, per-KRA ≥ 70%)'

Step 'Launching dashboard'
Write-Host 'Open another shell and run:  make dashboard'
Write-Host 'Then visit http://localhost:8501'

Step 'Smoke complete.'
Write-Host 'To tear down: make tf-destroy'

<#
.SYNOPSIS
  Copy your local ProcureIQ data (Docker Postgres + Docker Qdrant) to Neon and Qdrant Cloud.
  Windows PowerShell 5.1 compatible. Safe to re-run: the target is overwritten with the local state.

.DESCRIPTION
  Postgres:  pg_dump (inside your procureiq_postgres container) -> pg_restore into Neon
             -> create the read-only SQL role with the password you choose -> verify row counts
  Qdrant:    snapshot the local collection -> upload it to Qdrant Cloud -> verify point counts

  Needs: Docker Desktop with the local stack running (docker compose ... up -d), curl.exe (ships with
  Windows 10+), and the two cloud accounts' connection details.

.PARAMETER NeonUrl
  Neon connection string: postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
  (Neon console > Connect; use the DIRECT connection, not the pooled one, for the restore.)
.PARAMETER QdrantUrl      Qdrant Cloud cluster URL, e.g. https://xxxx.eu-central-1-0.aws.cloud.qdrant.io:6333
.PARAMETER QdrantApiKey   Qdrant Cloud database API key.
.PARAMETER RoPassword     Password for the new procureiq_ro role. Generated (and printed once) if omitted.
                          Also read from $env:PIQ_SECRET_POSTGRES_RO_PASSWORD, which deploy_gcp.ps1 -Action setup
                          picks up too, so you only enter it once.

.EXAMPLE
  .\infra\migrate_data.ps1 -NeonUrl 'postgresql://neondb_owner:***@ep-xxx.neon.tech/neondb?sslmode=require' `
      -QdrantUrl 'https://xxxx.cloud.qdrant.io:6333' -QdrantApiKey '***'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$NeonUrl,
    [Parameter(Mandatory = $true)][string]$QdrantUrl,
    [Parameter(Mandatory = $true)][string]$QdrantApiKey,
    [string]$RoPassword = $env:PIQ_SECRET_POSTGRES_RO_PASSWORD,
    [string]$LocalQdrant = "http://localhost:6333",
    [string]$PgContainer = "procureiq_postgres",
    [string]$Collection = "contract_chunks",
    [switch]$SkipPostgres,
    [switch]$SkipQdrant,
    [int]$MaxDatabaseMB = 480,   # Neon Free = 512 MB per project; keep a little headroom
    [switch]$Force      # migrate even if the database looks too big for Neon's free plan
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 leaves $PSScriptRoot EMPTY inside the param() block, so defaults that need the
# script folder are resolved here, in the body (with a fallback for unusual ways of starting the script).
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot = Split-Path $ScriptDir -Parent

function Invoke-Native([string]$Exe, [string[]]$NArgs) {
    # see deploy_gcp.ps1: PowerShell 5.1 treats any native stderr text as an error under Stop
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & $Exe @NArgs; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "$Exe $($NArgs[0..1] -join ' ') ... failed (exit code $code)" }
    return $out
}

function Get-PgScalar([string]$Sql, [string]$Target) {
    if ($Target -eq "local") { $a = @("exec", $PgContainer, "psql", "-U", "postgres", "-d", "procureiq", "-tAc", $Sql) }
    else { $a = @("exec", $PgContainer, "psql", $NeonUrl, "-tAc", $Sql) }
    return (Invoke-Native "docker" $a | Out-String).Trim()
}

$problems = @()

# ---------------------------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------------------------
if (-not $SkipPostgres) {
    Write-Host "== Postgres: local -> Neon =="
    if (-not $RoPassword) {
        $buf = New-Object byte[] 16
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
        $RoPassword = (($buf | ForEach-Object { $_.ToString("x2") }) -join "")   # 32 hex chars, cryptographically random
        Write-Host "generated procureiq_ro password: $RoPassword" -ForegroundColor Yellow
        Write-Host "  SAVE IT, then before running deploy_gcp.ps1 -Action setup:  `$env:PIQ_SECRET_POSTGRES_RO_PASSWORD = '$RoPassword'" -ForegroundColor Yellow
    }
    if ($RoPassword -notmatch '^[A-Za-z0-9_-]{16,}$') {
        throw "RoPassword must be 16+ characters of letters, digits, '-' or '_' (other characters break shell quoting)"
    }

    $size = [int64](Get-PgScalar "SELECT pg_database_size(current_database())" "local")
    Write-Host ("local database size: {0:N1} MB (Neon Free allows 512 MB per project)" -f ($size / 1MB))
    if ($size -gt ($MaxDatabaseMB * 1MB) -and -not $Force) {
        throw "The database is too big for Neon's free plan. Shrink it, upgrade the Neon plan, or re-run with -Force."
    }

    Write-Host "dumping..."
    Invoke-Native "docker" @("exec", $PgContainer, "pg_dump", "-U", "postgres", "-d", "procureiq", "--no-owner", "--no-acl", "-Fc", "-f", "/tmp/procureiq.dump") | Out-Null

    Write-Host "restoring into Neon (replaces same-named tables there)..."
    Invoke-Native "docker" @("exec", $PgContainer, "pg_restore", "--no-owner", "--no-acl", "--clean", "--if-exists", "-d", $NeonUrl, "/tmp/procureiq.dump") | Out-Null

    Write-Host "creating the read-only role procureiq_ro..."
    Invoke-Native "docker" @("cp", (Join-Path $RepoRoot "erp\create_readonly_role_cloud.sql"), "${PgContainer}:/tmp/create_readonly_role_cloud.sql") | Out-Null
    Invoke-Native "docker" @("exec", $PgContainer, "psql", $NeonUrl, "-v", "ON_ERROR_STOP=1", "-v", "ro_password=$RoPassword", "-q", "-f", "/tmp/create_readonly_role_cloud.sql") | Out-Null

    Write-Host "verifying row counts (local vs Neon)..."
    foreach ($t in @("vendors", "purchase_orders", "invoices", "contracts", "contract_chunks", "vendor_risk_screening")) {
        $l = Get-PgScalar "SELECT count(*) FROM $t" "local"
        $n = Get-PgScalar "SELECT count(*) FROM $t" "neon"
        if ($l -eq $n) { Write-Host ("  [PASS] {0,-24} {1}" -f $t, $n) -ForegroundColor Green }
        else { Write-Host ("  [FAIL] {0,-24} local={1} neon={2}" -f $t, $l, $n) -ForegroundColor Red; $problems += "postgres:$t" }
    }
    Invoke-Native "docker" @("exec", $PgContainer, "rm", "-f", "/tmp/procureiq.dump", "/tmp/create_readonly_role_cloud.sql") | Out-Null
}

# ---------------------------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------------------------
if (-not $SkipQdrant) {
    Write-Host "`n== Qdrant: local -> Qdrant Cloud =="
    $snapName = (Invoke-RestMethod -Method Post -Uri "$LocalQdrant/collections/$Collection/snapshots").result.name
    $file = Join-Path ([System.IO.Path]::GetTempPath()) $snapName
    try {
        Write-Host "snapshot: $snapName"
        Invoke-WebRequest -Uri "$LocalQdrant/collections/$Collection/snapshots/$snapName" -OutFile $file -UseBasicParsing
        Write-Host ("downloaded {0:N1} MB; uploading to Qdrant Cloud..." -f ((Get-Item $file).Length / 1MB))
        $resp = Invoke-Native "curl.exe" @("-sS", "-X", "POST", "$QdrantUrl/collections/$Collection/snapshots/upload?priority=snapshot",
            "-H", "api-key: $QdrantApiKey", "-F", "snapshot=@$file") | Out-String
        if ($resp -notmatch '"status"\s*:\s*"ok"') { throw "Qdrant Cloud did not accept the snapshot: $resp" }
    } finally {
        Remove-Item $file -Force -ErrorAction SilentlyContinue
        try { Invoke-RestMethod -Method Delete -Uri "$LocalQdrant/collections/$Collection/snapshots/$snapName" | Out-Null } catch { }
    }

    Write-Host "verifying point counts (local vs cloud)..."
    $body = '{"exact":true}'
    $l = (Invoke-RestMethod -Method Post -Uri "$LocalQdrant/collections/$Collection/points/count" -ContentType "application/json" -Body $body).result.count
    $c = (Invoke-RestMethod -Method Post -Uri "$QdrantUrl/collections/$Collection/points/count" -ContentType "application/json" -Body $body -Headers @{ "api-key" = $QdrantApiKey }).result.count
    if ($l -eq $c) { Write-Host ("  [PASS] {0,-24} {1}" -f $Collection, $c) -ForegroundColor Green }
    else { Write-Host ("  [FAIL] {0,-24} local={1} cloud={2}" -f $Collection, $l, $c) -ForegroundColor Red; $problems += "qdrant:$Collection" }
}

if ($problems.Count -gt 0) { Write-Host "`nMIGRATION INCOMPLETE: $($problems -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host "`nMIGRATION OK. Next: .\infra\deploy_gcp.ps1 -Action setup" -ForegroundColor Green
exit 0

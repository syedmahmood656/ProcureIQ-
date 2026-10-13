<#
  Smoke test for the running Docker stack (Phase 12).
    .\infra\smoke_test.ps1                      # uses http://localhost:8000
    .\infra\smoke_test.ps1 -ApiKey "mykey"      # if PROCUREIQ_API_KEY is set (or put it in $env:PROCUREIQ_API_KEY)
  Exit code 0 = everything works, 1 = something failed (the message says what).
#>
param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$ApiKey = $env:PROCUREIQ_API_KEY,
    [int]$ReadyTimeoutS = 240
)
$ErrorActionPreference = "Stop"
$failed = $false
function Pass($m) { Write-Host "[PASS] $m" -ForegroundColor Green }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red; $script:failed = $true }

$headers = @{}
if ($ApiKey) { $headers["X-API-Key"] = $ApiKey }

# 1) wait until /ready says everything is up (models load, Postgres, Qdrant, MCP SQL server)
Write-Host "Waiting for $BaseUrl/ready (up to $ReadyTimeoutS s; first start loads the models)..."
$deadline = (Get-Date).AddSeconds($ReadyTimeoutS)
$ready = $null
while ((Get-Date) -lt $deadline) {
    try {
        $ready = Invoke-RestMethod -Uri "$BaseUrl/ready" -Method Get
        if ($ready.status -eq "ready") { break }
    } catch { }
    Start-Sleep -Seconds 3
}
if ($ready -and $ready.status -eq "ready") {
    Pass "/ready -> $(($ready.checks.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', ')"
    if ($ready.checks.PSObject.Properties.Name -contains "mcp_sql") { Pass "SQL runs through the MCP server container" }
    else { Fail "/ready has no mcp_sql check: is PROCUREIQ_SQL_BACKEND=mcp reaching the api container?" }
} else {
    Fail "stack not ready after $ReadyTimeoutS s (last answer: $($ready | ConvertTo-Json -Compress)). Try: docker compose -f infra/docker-compose.yml logs api"
    exit 1
}

# 2) real questions through the whole pipeline (2 tries each: the free LLM tier is occasionally flaky)
function Ask($label, $question, $expectRoutes) {
    for ($try = 1; $try -le 2; $try++) {
        try {
            $body = @{ question = $question; refresh = $true } | ConvertTo-Json
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $r = Invoke-RestMethod -Uri "$BaseUrl/ask" -Method Post -ContentType "application/json" -Headers $headers -Body $body
            $sw.Stop()
            $rep = $r.report
            $line = "$label -> route=$($rep.route) verdict=$($rep.verdict) confidence=$($rep.confidence) ($([math]::Round($sw.Elapsed.TotalSeconds,1)) s)"
            if ($rep.verdict -eq "accept" -and ($expectRoutes -contains $rep.route)) { Pass $line; return }
            Write-Host "  try $try not good enough: $line" -ForegroundColor Yellow
        } catch {
            Write-Host "  try $try error: $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    Fail "$label failed twice"
}
Ask "SQL question     " "Who are the top 5 vendors by total spend?" @("sql")
Ask "Contract question" "Which contracts contain an uncapped liability clause?" @("rag", "both")

if ($failed) { Write-Host "`nSMOKE TEST FAILED" -ForegroundColor Red; exit 1 }
Write-Host "`nSMOKE TEST PASSED" -ForegroundColor Green
exit 0

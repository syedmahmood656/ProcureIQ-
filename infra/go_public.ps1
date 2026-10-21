<#
.SYNOPSIS
  Make your LOCAL ProcureIQ reachable from the internet for FREE (no cloud account, no card).
  Windows PowerShell 5.1 compatible.

.DESCRIPTION
  1. makes sure the API is protected by an API key (generates + saves one in .env if you have none)
  2. starts the Docker stack plus a free tunnel container (opens NO port on your PC or router;
     the tunnel only makes outbound connections)
  3. waits until the API is healthy, finds the public URL, checks it from the outside,
     runs the smoke test through it, and prints what to share

  -Provider cloudflare  (default) Cloudflare quick tunnel: no account at all. The URL is random and
                        CHANGES every time you start it. No uptime promise; no live streaming (/ask/stream).
  -Provider ngrok       permanent URL. Needs a free ngrok account: put NGROK_AUTHTOKEN and NGROK_DOMAIN
                        (your free static domain, e.g. my-name.ngrok-free.app) in .env.
  -Stop                 close public access (the API keeps running locally)
  -RotateKey            generate a NEW API key (use after sharing the old one)
  -NoVerify             start and print the URL, but skip the outside check and smoke test
  -PublicUrl <url>      skip URL detection and use this URL

  The public link works only while your PC is on and Docker is running.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\infra\go_public.ps1
  powershell -ExecutionPolicy Bypass -File .\infra\go_public.ps1 -Provider ngrok
  powershell -ExecutionPolicy Bypass -File .\infra\go_public.ps1 -Stop
#>
[CmdletBinding()]
param(
    [ValidateSet("cloudflare", "ngrok")][string]$Provider = "cloudflare",
    [switch]$Stop,
    [switch]$RotateKey,
    [switch]$NoVerify,
    [string]$PublicUrl,
    [string]$EnvFile = (Join-Path (Split-Path $PSScriptRoot -Parent) ".env"),
    [string]$ComposeFile = (Join-Path $PSScriptRoot "docker-compose.yml"),
    [int]$ReadyTimeoutS = 240,
    [int]$VerifyTimeoutS = 90
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
# Native commands run with ErrorAction=Continue: Windows PowerShell 5.1 turns any stderr text from a
# native command into a terminating error under "Stop", and docker writes progress to stderr.
function Invoke-Docker([string[]]$DArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & docker @DArgs 2>&1; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    return @{ Code = $code; Out = (($out | ForEach-Object { "$_" }) -join "`n") }
}

function Invoke-Compose([string[]]$Profiles, [string[]]$CArgs) {
    $a = @("compose", "-f", $ComposeFile, "--env-file", $EnvFile)
    foreach ($p in $Profiles) { $a += @("--profile", $p) }
    return (Invoke-Docker ($a + $CArgs))
}

function New-RandomHex([int]$Bytes = 24) {
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    return (($buf | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Read-EnvValue([string]$Key) {
    $fromProcess = [Environment]::GetEnvironmentVariable($Key)
    if ($fromProcess) { return $fromProcess }
    if (-not (Test-Path $EnvFile)) { return "" }
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        $t = $line.Trim()
        if ($t.StartsWith("#") -or -not $t.StartsWith("$Key=")) { continue }
        return $t.Substring($Key.Length + 1).Split("#")[0].Trim().Trim('"').Trim("'")
    }
    return ""
}

# Replace the KEY= line if there is one, otherwise append it. Every other byte of the file is kept.
function Set-EnvValue([string]$Key, [string]$Value) {
    $text = [System.IO.File]::ReadAllText($EnvFile)
    $pattern = "(?m)^$([regex]::Escape($Key))=.*$"
    if ([regex]::IsMatch($text, $pattern)) {
        $text = [regex]::Replace($text, $pattern, { param($m) "$Key=$Value" })
    } else {
        if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) { $text += "`r`n" }
        $text += "$Key=$Value`r`n"
    }
    [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# ---------------------------------------------------------------------------------------------
# -Stop
# ---------------------------------------------------------------------------------------------
$AllTunnelProfiles = @("app", "public", "public-ngrok")

if ($Stop) {
    Write-Host "Closing public access (the API keeps running locally)..."
    [void](Invoke-Compose $AllTunnelProfiles @("stop", "tunnel", "ngrok"))
    [void](Invoke-Compose $AllTunnelProfiles @("rm", "-f", "tunnel", "ngrok"))
    Write-Host "Done. The public link no longer works." -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------------------------
if ((Invoke-Docker @("info")).Code -ne 0) {
    throw "Docker is not running. Start Docker Desktop and wait until it says 'running', then try again."
}
if (-not (Test-Path $EnvFile)) {
    throw "No .env file at $EnvFile. Create it first:  Copy-Item .env.example .env  (then add your OPENROUTER_API_KEY)."
}

# ---- 1. never expose an unprotected API -------------------------------------------------------
$apiKey = Read-EnvValue "PROCUREIQ_API_KEY"
$keyIsNew = $false
if ($RotateKey -or -not $apiKey) {
    $apiKey = New-RandomHex 24
    Set-EnvValue "PROCUREIQ_API_KEY" $apiKey
    $env:PROCUREIQ_API_KEY = $apiKey
    $keyIsNew = $true
    Write-Host "New API key saved to .env (PROCUREIQ_API_KEY): $apiKey" -ForegroundColor Yellow
    Write-Host "  People need it in the X-API-Key header. Rotate it any time with -RotateKey." -ForegroundColor Yellow
}
if ($apiKey.Length -lt 16) { throw "PROCUREIQ_API_KEY is too short (need 16+ characters). Run again with -RotateKey." }

# ---- 2. provider settings ---------------------------------------------------------------------
if ($Provider -eq "ngrok") {
    $ngrokToken = Read-EnvValue "NGROK_AUTHTOKEN"
    $ngrokDomain = Read-EnvValue "NGROK_DOMAIN"
    if (-not $ngrokToken -or -not $ngrokDomain) {
        throw ("ngrok needs two lines in .env:`n  NGROK_AUTHTOKEN=<from dashboard.ngrok.com > Your Authtoken>`n  NGROK_DOMAIN=<your free static domain, from dashboard.ngrok.com > Domains>`n" +
               "Both are free. Or use the default provider (no account): re-run without -Provider.")
    }
    if ($ngrokDomain -notmatch '^[a-z0-9-]+(\.[a-z0-9-]+)+$') { throw "NGROK_DOMAIN must be a bare host name like my-name.ngrok-free.app (no https://)." }
    $profiles = @("app", "public-ngrok"); $service = "ngrok"; $otherService = "tunnel"
} else {
    $profiles = @("app", "public"); $service = "tunnel"; $otherService = "ngrok"
}

# ---- 3. start ---------------------------------------------------------------------------------
Write-Host "`n== starting the stack + $Provider tunnel =="
[void](Invoke-Compose $AllTunnelProfiles @("stop", $otherService))     # never leave two public paths open
$up = Invoke-Compose $profiles @("up", "-d")
if ($up.Code -ne 0) { throw "docker compose up failed:`n$($up.Out)" }

Write-Host -NoNewline "waiting for the API to become healthy (first start loads the models)"
$deadline = (Get-Date).AddSeconds($ReadyTimeoutS)
$health = ""
while ((Get-Date) -lt $deadline) {
    $health = (Invoke-Docker @("inspect", "-f", "{{.State.Health.Status}}", "procureiq_api")).Out.Trim()
    if ($health -eq "healthy") { break }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 3
}
Write-Host ""
if ($health -ne "healthy") {
    throw "The API did not become healthy within $ReadyTimeoutS s (status: '$health'). Look at: docker compose -f infra/docker-compose.yml --profile app logs api"
}

# ---- 4. find the public URL -------------------------------------------------------------------
$url = $PublicUrl
if (-not $url -and $Provider -eq "ngrok") { $url = "https://$ngrokDomain" }
if (-not $url) {
    Write-Host -NoNewline "waiting for Cloudflare to assign a public URL"
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not $url) {
        $logs = (Invoke-Docker @("logs", "procureiq_tunnel")).Out
        # newest match wins (logs accumulate across restarts); api.trycloudflare.com is Cloudflare's own endpoint, not ours
        $found = [regex]::Matches($logs, 'https://([a-z0-9-]+)\.trycloudflare\.com') | Where-Object { $_.Groups[1].Value -ne "api" }
        if ($found) { $url = @($found)[-1].Value; break }
        Write-Host -NoNewline "."
        Start-Sleep -Seconds 2
    }
    Write-Host ""
    if (-not $url) {
        throw "Could not find the public URL in the tunnel logs. Look at: docker logs procureiq_tunnel   (then re-run with -PublicUrl <the URL>)"
    }
}
$url = $url.TrimEnd("/")

# ---- 5. check it from the outside --------------------------------------------------------------
$extra = @{}
if ($Provider -eq "ngrok") { $extra["ngrok-skip-browser-warning"] = "1" }   # ngrok's free plan shows browsers an interstitial page

if (-not $NoVerify) {
    Write-Host "checking $url/ready from the outside (a brand-new tunnel needs a few seconds to resolve)..."
    $deadline = (Get-Date).AddSeconds($VerifyTimeoutS)
    $ok = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri "$url/ready" -Method Get -Headers $extra -TimeoutSec 15
            if ($r.status -eq "ready") { $ok = $true; break }
        } catch { }
        Start-Sleep -Seconds 3
    }
    if (-not $ok) { throw "The public URL did not answer /ready with status 'ready' within $VerifyTimeoutS s: $url" }
    Write-Host "[PASS] public /ready is ready" -ForegroundColor Green

    # real questions through the tunnel, using the API key (same test as for the cloud deployment)
    & (Join-Path $PSScriptRoot "smoke_test.ps1") -BaseUrl $url -ApiKey $apiKey -ExtraHeaders $extra
    if ($LASTEXITCODE -ne 0) { throw "The smoke test through the public URL failed (see above)." }
}

# ---- 6. what to share --------------------------------------------------------------------------
try { Set-Clipboard -Value $url } catch { }
Write-Host "`n==================== PUBLIC ====================" -ForegroundColor Green
Write-Host "URL:      $url   (copied to your clipboard)"
Write-Host "API key:  in .env as PROCUREIQ_API_KEY (send it as the X-API-Key header)"
Write-Host "`nTry it (PowerShell):"
Write-Host "  `$body = @{ question = 'Who are the top 5 vendors by total spend?' } | ConvertTo-Json"
Write-Host "  (Invoke-RestMethod -Uri '$url/ask' -Method Post -ContentType 'application/json' -Headers @{ 'X-API-Key' = '<key>' } -Body `$body).report"
Write-Host "`nRemember:"
if ($Provider -eq "cloudflare") { Write-Host "  - this URL changes the next time you start the tunnel; for a permanent URL use -Provider ngrok" }
Write-Host "  - it works only while this PC is on and Docker is running"
Write-Host "  - close it:  .\infra\go_public.ps1 -Stop        change the key:  .\infra\go_public.ps1 -RotateKey"
exit 0

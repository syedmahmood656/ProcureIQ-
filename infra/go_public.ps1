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
    [string]$TunnelUrlPattern = 'https://([a-z0-9-]+)\.trycloudflare\.com',   # group 1 = host label ('api' is ignored)
    [string]$EnvFile = "",          # default: .env in the repo root
    [string]$ComposeFile = "",      # default: infra\docker-compose.yml
    [int]$ReadyTimeoutS = 240,
    [int]$VerifyTimeoutS = 90
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 leaves $PSScriptRoot EMPTY inside the param() block, so defaults that need the
# script folder are resolved here, in the body (with a fallback for unusual ways of starting the script).
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path $ScriptDir -Parent) ".env" }
if (-not $ComposeFile) { $ComposeFile = Join-Path $ScriptDir "docker-compose.yml" }

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

# Newest tunnel URL in the tunnel container's logs. Logs accumulate across restarts, so the LAST match wins;
# api.trycloudflare.com is Cloudflare's own endpoint (it shows up in error lines), not our tunnel.
function Get-TunnelUrl([string]$Container, [int]$TimeoutS) {
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    while ((Get-Date) -lt $deadline) {
        $logs = (Invoke-Docker @("logs", $Container)).Out
        $found = [regex]::Matches($logs, $TunnelUrlPattern) | Where-Object { $_.Groups[1].Value -ne "api" }
        if ($found) { return @($found)[-1].Value }
        Write-Host -NoNewline "."
        Start-Sleep -Seconds 2
    }
    return ""
}

# True when ANY program on this PC is listening on the TCP port (works on Windows and Linux, IPv4 and IPv6).
function Test-PortListening([int]$Port) {
    $l = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() | Where-Object { $_.Port -eq $Port }
    return [bool]$l
}

# Names of RUNNING containers that publish the given HOST port. (`docker ps --filter publish=N` matches the
# container-side port, which would flag e.g. a container mapped 3000->8000, so the real mappings are read.)
function Get-PortHolders([string]$Port) {
    $holders = @()
    foreach ($line in ((Invoke-Docker @("ps", "--format", "{{.Names}}|{{.Ports}}")).Out -split "`n")) {
        $parts = $line.Trim().Split("|", 2)
        if ($parts.Count -eq 2 -and $parts[1] -match ":$Port->") { $holders += $parts[0] }
    }
    return $holders
}

# Plain-language hint when docker reports a busy port (an old container, or a local uvicorn started by hand).
function Get-PortHint([string]$Out) {
    if ($Out -match "port is already allocated|address already in use|Only one usage of each socket") {
        return "`nA port is already in use by something else (an old container, or a 'uvicorn' you started by hand). Find it with:`n  netstat -ano | findstr :8000`n  docker ps -a --filter publish=8000"
    }
    return ""
}

# One line about a container: running state, health, and the Docker networks it is on.
function Get-ContainerInfo([string]$Name) {
    $r = Invoke-Docker @("inspect", "-f", '{{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} networks={{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}', $Name)
    if ($r.Code -ne 0) { return "not found" }
    return $r.Out.Trim()
}

# One /ready call from the outside; says WHY it failed (HTTP status, DNS error, timeout...) instead of hiding it.
function Test-PublicReady([string]$Url, [hashtable]$Headers) {
    try {
        $r = Invoke-RestMethod -Uri "$Url/ready" -Method Get -Headers $Headers -TimeoutSec 15
        if ($r.status -eq "ready") { return @{ Ok = $true; Detail = "ready" } }
        return @{ Ok = $false; Detail = "answered, but status is '$($r.status)'" }
    } catch {
        $detail = $_.Exception.Message
        $resp = $_.Exception.Response
        if ($resp) { try { $detail = "HTTP " + [int]$resp.StatusCode } catch { } }
        return @{ Ok = $false; Detail = $detail }
    }
}

function Wait-PublicReady([string]$Url, [hashtable]$Headers, [int]$TimeoutS) {
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    $res = @{ Ok = $false; Detail = "no attempt made" }
    while ((Get-Date) -lt $deadline) {
        $res = Test-PublicReady $Url $Headers
        if ($res.Ok) { break }
        Start-Sleep -Seconds 3
    }
    return $res
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
    $profiles = @("app", "public-ngrok"); $service = "ngrok"; $otherService = "tunnel"; $logContainer = "procureiq_ngrok"
} else {
    $profiles = @("app", "public"); $service = "tunnel"; $otherService = "ngrok"; $logContainer = "procureiq_tunnel"
}

# ---- 2b. is the host port free? Otherwise the API container cannot start and the tunnel shows HTTP 502 ---
$hostPort = Read-EnvValue "PROCUREIQ_HOST_PORT"
if (-not $hostPort) { $hostPort = "8000" }
if ($hostPort -notmatch '^\d{2,5}$' -or [int]$hostPort -gt 65535) { throw "PROCUREIQ_HOST_PORT must be a port number such as 8010 (got '$hostPort')." }
$holders = @(Get-PortHolders $hostPort)
$foreign = @($holders | Where-Object { $_ -ne "procureiq_api" })
$portHelp = "`
Easiest fix, without touching the other program: add this line to your .env and run again:`
  PROCUREIQ_HOST_PORT=8010"
if ($foreign.Count -gt 0) {
    throw ("Port $hostPort on this PC is already used by another Docker container: $($foreign -join ', ').`
" +
           "ProcureIQ's API cannot start while that is true (and a tunnel in front of it would show HTTP 502).$portHelp`
" +
           "Or stop that container:  docker stop $($foreign[0])")
}
if (($holders -notcontains "procureiq_api") -and (Test-PortListening ([int]$hostPort))) {
    throw ("Port $hostPort on this PC is already used by a program that is not Docker (for example a 'uvicorn' started by hand).`
" +
           "Find it with:  netstat -ano | findstr :$hostPort$portHelp")
}

# ---- 3. start ---------------------------------------------------------------------------------
Write-Host "`n== starting the stack + $Provider tunnel =="
[void](Invoke-Compose $AllTunnelProfiles @("stop", $otherService))     # never leave two public paths open
$up = Invoke-Compose $profiles @("up", "-d")
if ($up.Code -ne 0) { throw "docker compose up failed:`n$($up.Out)$(Get-PortHint $up.Out)" }

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
    $url = Get-TunnelUrl $logContainer 90
    Write-Host ""
    if (-not $url) {
        throw "Could not find the public URL in the tunnel logs. Look at: docker logs $logContainer   (then re-run with -PublicUrl <the URL>)"
    }
}
$url = $url.TrimEnd("/")

# ---- 5. check it from the outside --------------------------------------------------------------
$extra = @{}
if ($Provider -eq "ngrok") { $extra["ngrok-skip-browser-warning"] = "1" }   # ngrok's free plan shows browsers an interstitial page

if (-not $NoVerify) {
    Write-Host "checking $url/ready from the outside (a brand-new tunnel needs a few seconds to resolve)..."
    $res = Wait-PublicReady $url $extra $VerifyTimeoutS

    if (-not $res.Ok -and $Provider -eq "cloudflare" -and -not $PublicUrl) {
        # A quick-tunnel address dies for good when the PC sleeps, the network changes or Docker restarts, but
        # the old container (and its old URL in the logs) is still there. Recreate it for a fresh address.
        Write-Host "The tunnel address is not answering ($($res.Detail))." -ForegroundColor Yellow
        if ($res.Detail -eq "HTTP 502") { Write-Host "HTTP 502 means the tunnel itself is up but cannot reach your API container." -ForegroundColor Yellow }
        $apiState = (Invoke-Docker @("inspect", "-f", "{{.State.Health.Status}}", "procureiq_api")).Out.Trim()
        if ($apiState -ne "healthy") {
            throw ("The API container is not healthy (status: '$apiState'), so a new tunnel would not help. Fix the API first:`n" +
                   "  docker compose -f infra/docker-compose.yml --env-file .env --profile app ps -a`n" +
                   "  docker compose -f infra/docker-compose.yml --env-file .env --profile app logs api`n" +
                   "If it cannot start because a port is busy:  netstat -ano | findstr :8000")
        }
        Write-Host "Quick-tunnel addresses do not survive a sleep, a network change or a Docker restart. Restarting the tunnel for a fresh address..." -ForegroundColor Yellow
        [void](Invoke-Compose $AllTunnelProfiles @("rm", "-f", "-s", "tunnel"))
        $re = Invoke-Compose $profiles @("up", "-d", "--no-deps", "tunnel")   # --no-deps: never touch (or recreate) the API container
        if ($re.Code -ne 0) { throw "Could not restart the tunnel:`n$($re.Out)$(Get-PortHint $re.Out)" }
        Write-Host -NoNewline "waiting for the new public URL"
        $newUrl = Get-TunnelUrl $logContainer 90
        Write-Host ""
        if (-not $newUrl) { throw "The restarted tunnel never printed a public URL. Look at: docker logs $logContainer" }
        $url = $newUrl.TrimEnd("/")
        Write-Host "checking $url/ready from the outside..."
        $res = Wait-PublicReady $url $extra $VerifyTimeoutS
    }

    if (-not $res.Ok) {
        $tail = (Invoke-Docker @("logs", "--tail", "8", $logContainer)).Out
        $why = ""
        if ($res.Detail -eq "HTTP 502") { $why = "HTTP 502 = Cloudflare reached your tunnel, but the tunnel could not reach http://api:8000. Both containers must be on the same network (listed below) and the API must be healthy.`n" }
        throw ("The public URL did not answer /ready with status 'ready' within $VerifyTimeoutS s: $url`n" +
               "Last result: $($res.Detail)`n$why`n" +
               "API container:    $(Get-ContainerInfo 'procureiq_api')`n" +
               "Tunnel container: $(Get-ContainerInfo $logContainer)`n`n" +
               "Tunnel log (last lines):`n$tail`n`n" +
               "Things to try:`n" +
               "  - wait a minute and run the script again (a new tunnel can take a moment to resolve)`n" +
               "  - 'name could not be resolved': a VPN or your DNS may block *.trycloudflare.com; turn the VPN off or try a phone hotspot`n" +
               "  - errors connecting to Cloudflare in the log: your network may block outbound port 7844; try another network`n" +
               "  - or use a permanent address instead: .\infra\go_public.ps1 -Provider ngrok")
    }
    Write-Host "[PASS] public /ready is ready" -ForegroundColor Green

    # real questions through the tunnel, using the API key (same test as for the cloud deployment)
    & (Join-Path $ScriptDir "smoke_test.ps1") -BaseUrl $url -ApiKey $apiKey -ExtraHeaders $extra
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

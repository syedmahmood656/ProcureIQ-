<#
.SYNOPSIS
  Deploy ProcureIQ to Google Cloud Run (Phase 13b). Windows PowerShell 5.1 compatible.

.DESCRIPTION
  -Action setup   one-time: enable APIs, create the image registry + runtime service account,
                  and store your secrets in Secret Manager (prompts; nothing is written to disk)
  -Action build   build the image in Google Cloud Build and push it to Artifact Registry
  -Action deploy  render the Cloud Run service YAML, deploy it, open it to the internet
                  (the API still needs its X-API-Key), then run infra/smoke_test.ps1 against it
  -Action all     build + deploy (default)      (run `setup` once before the first build)

  Settings come from infra\cloudrun\deploy.env (copy deploy.env.example). Secrets can be supplied
  non-interactively through environment variables named PIQ_SECRET_<NAME>, e.g.
  $env:PIQ_SECRET_OPENROUTER_API_KEY = "..."   (names are listed in the $Secrets table below).

.EXAMPLE
  .\infra\deploy_gcp.ps1 -Action setup
  .\infra\deploy_gcp.ps1                       # build + deploy
  .\infra\deploy_gcp.ps1 -Action deploy -Tag 20261013-120000    # redeploy / roll back to an existing build
#>
[CmdletBinding()]
param(
    [ValidateSet("setup", "build", "deploy", "all")]
    [string]$Action = "all",
    [string]$ConfigFile = "",       # default: infra\cloudrun\deploy.env
    [string]$Tag = (Get-Date -Format "yyyyMMdd-HHmmss"),
    [switch]$Private,      # do not allow unauthenticated invocation (then the smoke test cannot reach it)
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 leaves $PSScriptRoot EMPTY inside the param() block, so defaults that need the
# script folder are resolved here, in the body (with a fallback for unusual ways of starting the script).
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot = Split-Path $ScriptDir -Parent
if (-not $ConfigFile) { $ConfigFile = Join-Path $ScriptDir "cloudrun\deploy.env" }
# captured here because $PSBoundParameters inside a function is that function's own (empty) set
$TagWasGiven = $PSBoundParameters.ContainsKey("Tag")

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
function Read-EnvFile([string]$Path) {
    $cfg = @{}
    if (-not (Test-Path $Path)) { return $cfg }
    foreach ($line in Get-Content $Path) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $i = $t.IndexOf("=")
        $cfg[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Split("#")[0].Trim().Trim('"').Trim("'")
    }
    return $cfg
}

# Native commands: run with ErrorAction=Continue (Windows PowerShell 5.1 turns any stderr text from a
# native command into a terminating error otherwise, and gcloud writes progress to stderr), then
# check the exit code ourselves.
function Invoke-Gcloud([string[]]$GArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & gcloud @GArgs; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "gcloud $($GArgs -join ' ') failed (exit code $code)" }
}

function Get-GcloudValue([string[]]$GArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & gcloud @GArgs 2>$null; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "gcloud $($GArgs -join ' ') failed (exit code $code)" }
    return ($out | Out-String).Trim()
}

function Test-Gcloud([string[]]$GArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & gcloud @GArgs *> $null; $ok = ($LASTEXITCODE -eq 0) } finally { $ErrorActionPreference = $old }
    return $ok
}

function New-RandomHex([int]$Bytes = 24) {
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    return (($buf | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Read-SecretPrompt([string]$Prompt) {
    $sec = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# Writes the value to a temp file WITHOUT a trailing newline (piping to `gcloud ... --data-file=-`
# from PowerShell appends CR/LF, which silently breaks passwords and API keys).
function Set-SecretValue([string]$Name, [string]$Value, [string]$Project) {
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, $Value, (New-Object System.Text.UTF8Encoding($false)))
        if (Test-Gcloud @("secrets", "describe", $Name, "--project", $Project)) {
            Invoke-Gcloud @("secrets", "versions", "add", $Name, "--data-file=$tmp", "--project", $Project)
        } else {
            Invoke-Gcloud @("secrets", "create", $Name, "--replication-policy=automatic", "--data-file=$tmp", "--project", $Project)
        }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

# Secret Manager name, env-var suffix, prompt text, generate-if-empty?, required?
$Secrets = @(
    @{ Name = "procureiq-postgres-password";    Env = "POSTGRES_PASSWORD";    Prompt = "Neon owner password (the role in NEON_USER)"; Generate = $false; Optional = $false },
    @{ Name = "procureiq-postgres-ro-password"; Env = "POSTGRES_RO_PASSWORD"; Prompt = "Password of the procureiq_ro role (the one you gave migrate_data.ps1)"; Generate = $false; Optional = $false },
    @{ Name = "procureiq-qdrant-api-key";       Env = "QDRANT_API_KEY";       Prompt = "Qdrant Cloud database API key"; Generate = $false; Optional = $false },
    @{ Name = "procureiq-openrouter-api-key";   Env = "OPENROUTER_API_KEY";   Prompt = "OpenRouter API key"; Generate = $false; Optional = $false },
    @{ Name = "procureiq-api-key";              Env = "API_KEY";              Prompt = "API key clients must send in X-API-Key"; Generate = $true; Optional = $false },
    @{ Name = "procureiq-langfuse-public-key";  Env = "LANGFUSE_PUBLIC_KEY";  Prompt = "Langfuse public key"; Generate = $false; Optional = $true },
    @{ Name = "procureiq-langfuse-secret-key";  Env = "LANGFUSE_SECRET_KEY";  Prompt = "Langfuse secret key"; Generate = $false; Optional = $true }
)

# ---------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------
$cfg = Read-EnvFile $ConfigFile
if (-not (Test-Path $ConfigFile)) {
    throw "No config file at $ConfigFile. Copy infra\cloudrun\deploy.env.example to deploy.env and fill it in."
}
$Project = $cfg["PROJECT_ID"]
if (-not $Project) { throw "PROJECT_ID is missing in $ConfigFile" }
$Region = $cfg["REGION"]; if (-not $Region) { $Region = "asia-south1" }
$Service = $cfg["SERVICE_NAME"]; if (-not $Service) { $Service = "procureiq" }
$UseLangfuse = ($cfg["LANGFUSE"] -and @("1", "true", "yes", "on") -contains $cfg["LANGFUSE"].ToLower())
$RuntimeSa = "procureiq-run@$Project.iam.gserviceaccount.com"
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
$Repo = "procureiq"
$Image = "$Region-docker.pkg.dev/$Project/$Repo/app:$Tag"

Write-Host "Project: $Project   Region: $Region   Service: $Service"

# ---------------------------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------------------------
function Invoke-Setup {
    Write-Host "`n== setup: APIs =="
    Invoke-Gcloud @("services", "enable", "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "secretmanager.googleapis.com", "--project", $Project)

    Write-Host "`n== setup: image registry =="
    if (-not (Test-Gcloud @("artifacts", "repositories", "describe", $Repo, "--location", $Region, "--project", $Project))) {
        Invoke-Gcloud @("artifacts", "repositories", "create", $Repo, "--repository-format=docker", "--location", $Region, "--project", $Project)
    } else { Write-Host "registry '$Repo' already exists" }

    Write-Host "`n== setup: runtime service account =="
    if (-not (Test-Gcloud @("iam", "service-accounts", "describe", $RuntimeSa, "--project", $Project))) {
        Invoke-Gcloud @("iam", "service-accounts", "create", "procureiq-run", "--display-name", "ProcureIQ runtime", "--project", $Project)
    } else { Write-Host "$RuntimeSa already exists" }

    Write-Host "`n== setup: let Cloud Build push images (new projects use the default compute account) =="
    $num = Get-GcloudValue @("projects", "describe", $Project, "--format=value(projectNumber)")
    $buildSa = "serviceAccount:$num-compute@developer.gserviceaccount.com"
    foreach ($role in @("roles/cloudbuild.builds.builder", "roles/artifactregistry.writer")) {
        Invoke-Gcloud @("projects", "add-iam-policy-binding", $Project, "--member=$buildSa", "--role=$role", "--quiet")
    }

    Write-Host "`n== setup: secrets (Secret Manager) =="
    foreach ($s in $Secrets) {
        if ($s.Optional -and -not $UseLangfuse) { continue }
        $name = $s.Name
        $value = [Environment]::GetEnvironmentVariable("PIQ_SECRET_$($s.Env)")
        $exists = Test-Gcloud @("secrets", "describe", $name, "--project", $Project)
        if (-not $value) {
            if ($s.Generate -and -not $exists) {
                $value = New-RandomHex 24
                Write-Host "generated $name = $value   <-- SAVE THIS: it is the X-API-Key for your API (shown once)" -ForegroundColor Yellow
            } elseif ($Interactive) {
                $suffix = ""; if ($exists) { $suffix = " (Enter = keep the existing value)" }
                $value = Read-SecretPrompt ("$($s.Prompt)$suffix")
            }
            # not interactive (piped input / CI): never prompt; an existing secret is kept, a missing one fails below
        }
        if ($value) {
            Set-SecretValue $name $value $Project
            Write-Host "stored $name"
        } elseif (-not $exists) {
            throw "$name has no value and does not exist yet. Set `$env:PIQ_SECRET_$($s.Env) and re-run."
        } else { Write-Host "kept existing $name" }
        Invoke-Gcloud @("secrets", "add-iam-policy-binding", $name, "--member=serviceAccount:$RuntimeSa", "--role=roles/secretmanager.secretAccessor", "--project", $Project, "--quiet")
    }
    Write-Host "`nsetup complete. Next: .\infra\deploy_gcp.ps1" -ForegroundColor Green
}

# ---------------------------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------------------------
function Invoke-Build {
    Write-Host "`n== build: $Image =="
    Push-Location $RepoRoot
    try {
        Invoke-Gcloud @("builds", "submit", "--config", "infra/cloudbuild.yaml", "--substitutions", "_IMAGE=$Image", "--project", $Project, ".")
    } finally { Pop-Location }
}

# ---------------------------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------------------------
function Invoke-Deploy {
    if ($Action -eq "deploy" -and -not $TagWasGiven) {
        throw "-Action deploy needs -Tag <an existing build>. List builds with: gcloud artifacts docker images list $Region-docker.pkg.dev/$Project/$Repo"
    }
    Write-Host "`n== deploy: $Image =="
    $rendered = Join-Path $ScriptDir "cloudrun\service.rendered.yaml"
    $py = "python"
    & $py (Join-Path $ScriptDir "render_service.py") --config $ConfigFile --out $rendered --image $Image --region $Region --runtime-sa $RuntimeSa
    if ($LASTEXITCODE -ne 0) { throw "render_service.py failed (see the config error above)" }

    Invoke-Gcloud @("run", "services", "replace", $rendered, "--region", $Region, "--project", $Project)

    if (-not $Private) {
        Invoke-Gcloud @("run", "services", "add-iam-policy-binding", $Service, "--region", $Region, "--member=allUsers", "--role=roles/run.invoker", "--project", $Project, "--quiet")
    }
    $url = Get-GcloudValue @("run", "services", "describe", $Service, "--region", $Region, "--project", $Project, "--format=value(status.url)")
    Write-Host "`nService URL: $url" -ForegroundColor Green
    Write-Host "Add it as the repository variable SERVICE_URL so the daily keep-alive can ping it."

    if ($SkipSmoke -or $Private) { return }
    Write-Host "`n== smoke test (first request cold-starts the instance: allow 1-3 minutes) =="
    $apiKey = Get-GcloudValue @("secrets", "versions", "access", "latest", "--secret", "procureiq-api-key", "--project", $Project)
    $hostExe = (Get-Process -Id $PID).Path
    & $hostExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "smoke_test.ps1") -BaseUrl $url -ApiKey $apiKey
    if ($LASTEXITCODE -ne 0) { throw "smoke test failed: check 'gcloud run services logs read $Service --region $Region'" }
}

switch ($Action) {
    "setup"  { Invoke-Setup }
    "build"  { Invoke-Build }
    "deploy" { Invoke-Deploy }
    "all"    { Invoke-Build; Invoke-Deploy }
}

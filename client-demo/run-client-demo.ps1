$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'backend'
$demoAppDir = Join-Path $repoRoot 'demo-app'
$python = Join-Path $backendDir '.venv\Scripts\python.exe'
$envFile = Join-Path $backendDir '.env'
$apiUrl = 'http://127.0.0.1:8000'
$shopUrl = 'http://127.0.0.1:4173'
$logDir = Join-Path $env:TEMP 'OmniSightClientDemo'
$processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()

function Read-EnvValue([string] $Name, [string[]] $Lines) {
    $prefix = '^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*)$'
    for ($index = $Lines.Length - 1; $index -ge 0; $index--) {
        if ($Lines[$index] -match $prefix) {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[$value.Length - 1] -eq '"') -or ($value[0] -eq "'" -and $value[$value.Length - 1] -eq "'"))) {
                return $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return ''
}

function Wait-Http([string] $Url, [int] $Seconds, [System.Diagnostics.Process[]] $Started) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        foreach ($process in $Started) {
            if ($process.HasExited) { throw "A demo service exited early (PID $($process.Id)). Check logs in $logDir." }
        }
        try {
            $null = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
            return
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "Timed out waiting for $Url. Check logs in $logDir."
}

if (-not (Test-Path -LiteralPath $python)) { throw 'Backend virtualenv not found. Create backend\.venv and install backend\requirements.txt first.' }
if (-not (Test-Path -LiteralPath $envFile)) { throw 'backend\.env is missing. Copy backend\.env.example and configure the Gemini and ShopLite demo settings.' }
if (-not (Test-Path -LiteralPath (Join-Path $demoAppDir 'checkout\index.html'))) { throw 'ShopLite demo app is missing.' }

$envLines = Get-Content -LiteralPath $envFile
$apiKey = Read-EnvValue 'OMNISIGHT_GEMINI_API_KEY' $envLines
$sandboxToken = Read-EnvValue 'OMNISIGHT_SHOPLITE_SANDBOX_TOKEN' $envLines
$vlm = Read-EnvValue 'OMNISIGHT_VLM_ADAPTER' $envLines
$generator = Read-EnvValue 'OMNISIGHT_PATCH_GENERATOR' $envLines
if (-not $apiKey -or -not $sandboxToken) { throw 'backend\.env must have a Gemini API key and ShopLite sandbox approval token. Values are not displayed.' }
if ($vlm -ne 'gemini' -or $generator -ne 'gemini') { throw 'Set OMNISIGHT_VLM_ADAPTER=gemini and OMNISIGHT_PATCH_GENERATOR=gemini in backend\.env.' }

& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is unavailable. Start Docker Desktop and try again.' }
& docker image inspect omnisight-shoplite-sandbox:1 *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Building the fixed ShopLite sandbox image (first run may take a few minutes)...'
    & docker build --tag omnisight-shoplite-sandbox:1 --file (Join-Path $demoAppDir 'sandbox\Dockerfile') (Join-Path $demoAppDir 'sandbox')
    if ($LASTEXITCODE -ne 0) { throw 'Docker could not build the ShopLite sandbox image.' }
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$shopLog = Join-Path $logDir 'shoplite.log'
$apiLog = Join-Path $logDir 'api.log'
$shopErrorLog = Join-Path $logDir 'shoplite-error.log'
$apiErrorLog = Join-Path $logDir 'api-error.log'
$shopProcess = $null
$apiProcess = $null
try {
    $shopProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'http.server', '4173', '--bind', '127.0.0.1') -WorkingDirectory $demoAppDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $shopLog -RedirectStandardError $shopErrorLog
    $processes.Add($shopProcess)
    $apiProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $backendDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog
    $processes.Add($apiProcess)

    Write-Host 'Starting the local ShopLite target and OmniSight API...'
    Wait-Http "$shopUrl/checkout/" 30 @($shopProcess)
    Wait-Http "$apiUrl/health" 30 @($shopProcess, $apiProcess)

    $env:OMNISIGHT_SHOPLITE_SANDBOX_TOKEN = $sandboxToken
    Write-Host 'Running live Gemini detection, safe patch proposal, and isolated Docker verification...'
    Push-Location $backendDir
    try {
        & $python scripts\run_demo_pipeline.py --checkout-url "$shopUrl/checkout" --api-url $apiUrl --timeout 180 --approve-sandbox
        $runExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($runExitCode -ne 0) { throw "The demo did not return verified status (exit $runExitCode). See the summary under backend\artifacts\demo-checkout and logs in $logDir." }
    Write-Host 'Verified. Opening the client presentation page.'
    Start-Process -FilePath (Join-Path $PSScriptRoot 'index.html')
} finally {
    Remove-Item Env:OMNISIGHT_SHOPLITE_SANDBOX_TOKEN -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

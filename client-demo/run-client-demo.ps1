$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'backend'
$demoAppDir = Join-Path $repoRoot 'demo-app'
$python = Join-Path $backendDir '.venv\Scripts\python.exe'
$envFile = Join-Path $backendDir '.env'
$apiUrl = 'http://127.0.0.1:8000'
$logDir = Join-Path $env:TEMP 'OmniSightClientDemo'
$processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$dbContainerName = 'omnisight-shoplite-postgres'
$dbStartedForDemo = $false
$previousDatabaseUrl = $env:OMNISIGHT_DATABASE_URL

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
$sandboxEnabled = Read-EnvValue 'OMNISIGHT_ENABLE_SHOPLITE_SANDBOX' $envLines
$databaseUrl = $env:OMNISIGHT_DATABASE_URL
if (-not $databaseUrl) { $databaseUrl = Read-EnvValue 'OMNISIGHT_DATABASE_URL' $envLines }
if ($databaseUrl -match 'CHANGE_ME') { $databaseUrl = '' }
$vlm = Read-EnvValue 'OMNISIGHT_VLM_ADAPTER' $envLines
$generator = Read-EnvValue 'OMNISIGHT_PATCH_GENERATOR' $envLines
if (-not $apiKey -or -not $sandboxToken) { throw 'backend\.env must have a Gemini API key and ShopLite sandbox approval token. Values are not displayed.' }
if ($sandboxEnabled -ne 'true') { throw 'Set OMNISIGHT_ENABLE_SHOPLITE_SANDBOX=true in backend\.env to enable the approved demo verification step.' }
if ($vlm -ne 'gemini' -or $generator -ne 'gemini') { throw 'Set OMNISIGHT_VLM_ADAPTER=gemini and OMNISIGHT_PATCH_GENERATOR=gemini in backend\.env.' }

& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is unavailable. Start Docker Desktop and try again.' }

try {
if (-not $databaseUrl) {
    $null = & docker inspect $dbContainerName 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Starting the local PostgreSQL database (data is kept in a Docker volume)...'
        & docker run --detach --name $dbContainerName --publish '127.0.0.1:55432:5432' --env 'POSTGRES_DB=omnisight' --env 'POSTGRES_HOST_AUTH_METHOD=trust' --volume 'omnisight-shoplite-postgres-data:/var/lib/postgresql/data' postgres:16-alpine
        if ($LASTEXITCODE -ne 0) { throw 'Could not start local PostgreSQL. Check whether port 55432 is available.' }
        $dbStartedForDemo = $true
    } else {
        $dbRunning = (& docker inspect --format '{{.State.Running}}' $dbContainerName).Trim()
        if ($dbRunning -ne 'true') {
            & docker start $dbContainerName *> $null
            if ($LASTEXITCODE -ne 0) { throw 'Could not start the existing OmniSight PostgreSQL container.' }
            $dbStartedForDemo = $true
        }
    }

    $databaseReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        & docker exec $dbContainerName pg_isready -U postgres -d omnisight *> $null
        if ($LASTEXITCODE -eq 0) { $databaseReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $databaseReady) { throw 'Local PostgreSQL did not become ready. Check Docker logs for the database container.' }
    $databaseUrl = 'postgresql+psycopg://postgres@127.0.0.1:55432/omnisight'
}
$env:OMNISIGHT_DATABASE_URL = $databaseUrl

Write-Host 'Applying database migrations...'
Push-Location $backendDir
try {
    & $python -m alembic upgrade head
    $migrationExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($migrationExitCode -ne 0) { throw 'Database migrations failed. Check the PostgreSQL service and database configuration.' }
& docker image inspect omnisight-shoplite-sandbox:1 *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Building the fixed ShopLite sandbox image (first run may take a few minutes)...'
    & docker build --tag omnisight-shoplite-sandbox:1 --file (Join-Path $demoAppDir 'sandbox\Dockerfile') (Join-Path $demoAppDir 'sandbox')
    if ($LASTEXITCODE -ne 0) { throw 'Docker could not build the ShopLite sandbox image.' }
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$apiLog = Join-Path $logDir 'api.log'
$apiErrorLog = Join-Path $logDir 'api-error.log'
$apiProcess = $null
    $apiProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $backendDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog
    $processes.Add($apiProcess)

    Write-Host 'Starting OmniSight with the local ShopLite workflow...'
    Wait-Http "$apiUrl/health" 30 @($apiProcess)
    Write-Host 'Opening the connected dashboard.'
    Start-Process -FilePath $apiUrl
    Write-Host 'Click Live ShopLite Demo, start a scan, inspect the finding and patch, then type approve-demo-only to run sandbox verification.'
    Write-Host 'Run history and evidence metadata are stored in local PostgreSQL. Data remains in the Docker volume between sessions.'
    Read-Host 'Press Enter here when you are done to stop OmniSight and PostgreSQL'
} finally {
    Remove-Item Env:OMNISIGHT_SHOPLITE_SANDBOX_TOKEN -ErrorAction SilentlyContinue
    if ($null -eq $previousDatabaseUrl) { Remove-Item Env:OMNISIGHT_DATABASE_URL -ErrorAction SilentlyContinue }
    else { $env:OMNISIGHT_DATABASE_URL = $previousDatabaseUrl }
    foreach ($process in $processes) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if ($dbStartedForDemo) { & docker stop $dbContainerName *> $null }
}

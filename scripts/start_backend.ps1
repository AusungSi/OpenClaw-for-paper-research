$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-ProjectRoot
Set-Location $projectRoot

$envPath = Ensure-DotEnv
$envMap = Import-DotEnv -Path $envPath

$condaEnvName = $envMap["CONDA_ENV_NAME"]
if ([string]::IsNullOrWhiteSpace($condaEnvName)) {
    $condaEnvName = "memomate"
}

$pythonExe = $null
$condaCmd = Get-Command conda -ErrorAction SilentlyContinue
if ($null -ne $condaCmd) {
    try {
        $condaBase = (& $condaCmd.Source info --base).Trim()
        if (-not [string]::IsNullOrWhiteSpace($condaBase)) {
            $candidate = Join-Path $condaBase "envs\\$condaEnvName\\python.exe"
            if (Test-Path $candidate) {
                $pythonExe = $candidate
            }
        }
    } catch {
    }
}

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    $candidate = Join-Path $HOME "anaconda3\\envs\\$condaEnvName\\python.exe"
    if (Test-Path $candidate) {
        $pythonExe = $candidate
    }
}

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    throw "Cannot find python for conda env '$condaEnvName'. Set CONDA_ENV_NAME in .env or install the env."
}

Write-Host "[Backend] Using python: $pythonExe"
$appHost = $envMap["APP_HOST"]
if ([string]::IsNullOrWhiteSpace($appHost)) {
    $appHost = "0.0.0.0"
}

$appPort = $envMap["APP_PORT"]
if ([string]::IsNullOrWhiteSpace($appPort)) {
    $appPort = "8000"
}

Write-Host "[Backend] Starting app.main:app on $appHost`:$appPort"
& $pythonExe -m uvicorn app.main:app --host $appHost --port $appPort

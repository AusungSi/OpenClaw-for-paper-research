param(
    [switch]$SkipTests,
    [switch]$OnlyTest,
    [switch]$BackendOnly
)

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

Write-Host "[Python] Using: $pythonExe"

if (-not $SkipTests) {
    Write-Host "[Test] Running pytest..."
    & $pythonExe -m pytest -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) {
        throw "Tests failed, startup aborted."
    }
    Write-Host "[Test] Passed."
}

if ($OnlyTest) {
    Write-Host "[Run] OnlyTest enabled, exit."
    exit 0
}

if ($BackendOnly) {
    Write-Host "[Run] Starting backend only..."
    & (Join-Path $PSScriptRoot "start_backend.ps1")
    exit $LASTEXITCODE
}

Write-Host "[Run] Starting backend + tunnel..."
& (Join-Path $PSScriptRoot "start_all.ps1")
exit $LASTEXITCODE

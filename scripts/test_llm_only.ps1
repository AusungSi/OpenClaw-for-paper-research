param(
    [string]$IntentText = "明天早上9点提醒我开会",
    [string]$Timezone = "Asia/Shanghai",
    [switch]$SkipPytest
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
            $candidate = Join-Path $condaBase "envs\$condaEnvName\python.exe"
            if (Test-Path $candidate) {
                $pythonExe = $candidate
            }
        }
    } catch {
    }
}

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    $candidate = Join-Path $HOME "anaconda3\envs\$condaEnvName\python.exe"
    if (Test-Path $candidate) {
        $pythonExe = $candidate
    }
}

if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    throw "Cannot find python for conda env '$condaEnvName'. Set CONDA_ENV_NAME in .env or install the env."
}

Write-Host "[Python] Using: $pythonExe"
Write-Host "[LLM] Intent playground..."
& $pythonExe .\scripts\llm_playground.py intent --text $IntentText --timezone $Timezone

if (-not $SkipPytest) {
    Write-Host "[LLM] Running intent/reply contract tests..."
    & $pythonExe -m pytest -q tests/test_llm_intent_contract.py tests/test_llm_reply_contract.py
}

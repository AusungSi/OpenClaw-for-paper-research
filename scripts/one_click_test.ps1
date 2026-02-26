$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-ProjectRoot
$envPath = Ensure-DotEnv
$envMap = Import-DotEnv -Path $envPath

$mode = $envMap["CF_TUNNEL_MODE"]
if ([string]::IsNullOrWhiteSpace($mode)) {
    $mode = "quick"
}

$needsNamedSetup = $false
if ($mode -eq "named") {
    $needsNamedSetup = (
        [string]::IsNullOrWhiteSpace($envMap["CF_TUNNEL_HOSTNAME"]) `
        -or ($envMap["CF_TUNNEL_HOSTNAME"] -like "*.example.com") `
        -or [string]::IsNullOrWhiteSpace($envMap["CF_TUNNEL_ID"]) `
        -or -not (Test-Path (Join-Path $projectRoot ".cloudflared\config.yml"))
    )
}

if ($needsNamedSetup) {
    Write-Host "[Setup] First-time tunnel setup required." -ForegroundColor Yellow
    Write-Host "[Setup] You need a domain managed by Cloudflare."
    $defaultHost = "memomate.yourdomain.com"
    $inputHost = Read-Host "Enter fixed hostname (e.g. $defaultHost)"
    if ([string]::IsNullOrWhiteSpace($inputHost)) {
        throw "Hostname is required."
    }
    & (Join-Path $PSScriptRoot "setup_named_tunnel.ps1") -Hostname $inputHost
    if ($LASTEXITCODE -ne 0) {
        throw "Named tunnel setup failed."
    }
}

Write-Host "[Run] Starting backend and tunnel..." -ForegroundColor Green
& (Join-Path $PSScriptRoot "start_all.ps1")

$envMap = Import-DotEnv -Path $envPath
$callback = $envMap["WECOM_CALLBACK_URL"]
if (-not [string]::IsNullOrWhiteSpace($callback)) {
    Write-Host ""
    Write-Host "WeCom callback URL (stable): $callback"
}
Write-Host "Use this script next time: .\\scripts\\one_click_test.ps1"

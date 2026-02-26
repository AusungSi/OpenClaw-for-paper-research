param(
    [switch]$Quick
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-ProjectRoot
$cloudflared = Get-CloudflaredBinary
$envMap = Import-DotEnv -Path (Get-DotEnvPath)

$localUrl = $envMap["CF_TUNNEL_LOCAL_URL"]
if ([string]::IsNullOrWhiteSpace($localUrl)) {
    $localUrl = "http://localhost:8000"
}

$mode = $envMap["CF_TUNNEL_MODE"]
$configPath = $envMap["CF_TUNNEL_CONFIG_FILE"]
if (-not [string]::IsNullOrWhiteSpace($configPath)) {
    $configPath = Join-Path $projectRoot $configPath
}

$hostname = $envMap["CF_TUNNEL_HOSTNAME"]
$tunnelId = $envMap["CF_TUNNEL_ID"]

$canUseNamed = (
    -not $Quick `
    -and $mode -eq "named" `
    -and -not [string]::IsNullOrWhiteSpace($hostname) `
    -and -not [string]::IsNullOrWhiteSpace($tunnelId) `
    -and (Test-Path $configPath)
)

if ($canUseNamed) {
    Write-Host "[Tunnel] Using named tunnel (stable URL)." -ForegroundColor Green
    Write-Host "[Tunnel] Callback URL: https://$hostname/wechat"
    & $cloudflared tunnel --config $configPath run $tunnelId
    exit $LASTEXITCODE
}

Write-Host "[Tunnel] Using quick tunnel (URL will change every run)." -ForegroundColor Yellow
Write-Host "[Tunnel] Run scripts/setup_named_tunnel.ps1 once to get a fixed URL."
& $cloudflared tunnel --url $localUrl
exit $LASTEXITCODE

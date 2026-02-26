param(
    [string]$TunnelName,
    [string]$Hostname,
    [string]$LocalUrl
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")

$projectRoot = Get-ProjectRoot
$envPath = Ensure-DotEnv
$envMap = Import-DotEnv -Path $envPath

if ([string]::IsNullOrWhiteSpace($TunnelName)) {
    $TunnelName = $envMap["CF_TUNNEL_NAME"]
}
if ([string]::IsNullOrWhiteSpace($TunnelName)) {
    $TunnelName = "memomate"
}

if ([string]::IsNullOrWhiteSpace($LocalUrl)) {
    $LocalUrl = $envMap["CF_TUNNEL_LOCAL_URL"]
}
if ([string]::IsNullOrWhiteSpace($LocalUrl)) {
    $LocalUrl = "http://localhost:8000"
}

if ([string]::IsNullOrWhiteSpace($Hostname)) {
    $Hostname = $envMap["CF_TUNNEL_HOSTNAME"]
}

if ([string]::IsNullOrWhiteSpace($Hostname) -or $Hostname -like "*.example.com") {
    throw "Missing valid hostname. Provide -Hostname memomate.yourdomain.com"
}

$cloudflared = Get-CloudflaredBinary
$certPath = Join-Path $HOME ".cloudflared\cert.pem"

if (-not (Test-Path $certPath)) {
    Write-Host "[Cloudflare] Login required. Browser will open once." -ForegroundColor Yellow
    & $cloudflared tunnel login
    if ($LASTEXITCODE -ne 0) {
        throw "cloudflared tunnel login failed."
    }
}

function Get-TunnelByName {
    param([string]$Name)

    $json = & $cloudflared tunnel list -o json
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to list tunnels."
    }
    $all = $json | ConvertFrom-Json
    return $all | Where-Object { $_.name -eq $Name } | Select-Object -First 1
}

$tunnel = Get-TunnelByName -Name $TunnelName
if ($null -eq $tunnel) {
    Write-Host "[Cloudflare] Creating tunnel: $TunnelName"
    & $cloudflared tunnel create $TunnelName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create tunnel."
    }
    $tunnel = Get-TunnelByName -Name $TunnelName
}

if ($null -eq $tunnel) {
    throw "Tunnel '$TunnelName' not found after create."
}

$tunnelId = $tunnel.id
$cfDir = Join-Path $projectRoot ".cloudflared"
New-Item -ItemType Directory -Path $cfDir -Force | Out-Null
$credFile = Join-Path $cfDir "$TunnelName.json"
$configFile = Join-Path $cfDir "config.yml"

Write-Host "[Cloudflare] Binding hostname: $Hostname"
& $cloudflared tunnel route dns -f $TunnelName $Hostname
if ($LASTEXITCODE -ne 0) {
    throw "Failed to bind DNS route."
}

Write-Host "[Cloudflare] Fetching tunnel token to credentials file."
& $cloudflared tunnel token --cred-file $credFile $TunnelName
if ($LASTEXITCODE -ne 0) {
    throw "Failed to fetch tunnel token."
}

$configText = @"
tunnel: $tunnelId
credentials-file: $credFile
ingress:
  - hostname: $Hostname
    service: $LocalUrl
  - service: http_status:404
"@
Set-Content -Path $configFile -Value $configText -Encoding UTF8

Set-DotEnvValue -Path $envPath -Key "CF_TUNNEL_MODE" -Value "named"
Set-DotEnvValue -Path $envPath -Key "CF_TUNNEL_NAME" -Value $TunnelName
Set-DotEnvValue -Path $envPath -Key "CF_TUNNEL_ID" -Value $tunnelId
Set-DotEnvValue -Path $envPath -Key "CF_TUNNEL_HOSTNAME" -Value $Hostname
Set-DotEnvValue -Path $envPath -Key "CF_TUNNEL_LOCAL_URL" -Value $LocalUrl
Set-DotEnvValue -Path $envPath -Key "CF_TUNNEL_CONFIG_FILE" -Value ".cloudflared/config.yml"
Set-DotEnvValue -Path $envPath -Key "WECOM_CALLBACK_URL" -Value "https://$Hostname/wechat"

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Stable callback URL:"
Write-Host "  https://$Hostname/wechat"
Write-Host ""
Write-Host "You only need to configure WeCom callback once with that URL."

$ErrorActionPreference = "Stop"

function Get-ProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-CloudflaredBinary {
    $projectRoot = Get-ProjectRoot
    $localBinary = Join-Path $projectRoot "cloudflared.exe"
    if (Test-Path $localBinary) {
        return $localBinary
    }

    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    throw "cloudflared binary not found. Put cloudflared.exe under project root or add cloudflared to PATH."
}

function Get-DotEnvPath {
    $projectRoot = Get-ProjectRoot
    return Join-Path $projectRoot ".env"
}

function Import-DotEnv {
    param(
        [string]$Path = (Get-DotEnvPath)
    )

    $map = @{}
    if (-not (Test-Path $Path)) {
        return $map
    }

    $lines = Get-Content -Path $Path -Encoding UTF8
    foreach ($line in $lines) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed)) { continue }
        if ($trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -le 0) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim()
        $map[$key] = $value
    }
    return $map
}

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [string]$Path = (Get-DotEnvPath)
    )

    if (-not (Test-Path $Path)) {
        New-Item -ItemType File -Path $Path | Out-Null
    }

    $lines = @(Get-Content -Path $Path -Encoding UTF8)
    $pattern = "^$([Regex]::Escape($Key))="
    $updated = $false

    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $updated = $true
            break
        }
    }

    if (-not $updated) {
        $lines += "$Key=$Value"
    }

    Set-Content -Path $Path -Value $lines -Encoding UTF8
}

function Ensure-DotEnv {
    $projectRoot = Get-ProjectRoot
    $envPath = Join-Path $projectRoot ".env"
    $envExamplePath = Join-Path $projectRoot ".env.example"
    if (-not (Test-Path $envPath)) {
        if (-not (Test-Path $envExamplePath)) {
            throw ".env and .env.example are both missing."
        }
        Copy-Item -Path $envExamplePath -Destination $envPath
    }
    return $envPath
}


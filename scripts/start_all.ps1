$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$backendScript = Join-Path $projectRoot "scripts\\start_backend.ps1"
$tunnelScript = Join-Path $projectRoot "scripts\\start_tunnel.ps1"

Start-Process powershell -WorkingDirectory $projectRoot -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $backendScript
Start-Sleep -Seconds 2
Start-Process powershell -WorkingDirectory $projectRoot -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $tunnelScript

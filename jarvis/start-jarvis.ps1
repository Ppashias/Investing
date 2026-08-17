<#
.SYNOPSIS
    Start the JARVIS backend locally.

.DESCRIPTION
    Runs the API and the web UI on 127.0.0.1. Loopback only — JARVIS is not
    intended to be reachable from your network, and binding it there would
    expose your vault and your API token to anything on the same Wi-Fi.

    Run .\setup-windows.ps1 once first.

.PARAMETER Port
    Port to listen on. Default 8787.

.PARAMETER Reload
    Restart on source changes. For development only.

.EXAMPLE
    .\start-jarvis.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 8787,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "No virtual environment found." -ForegroundColor Red
    Write-Host "Run .\setup-windows.ps1 first."
    exit 1
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Write-Host "No .env found. Run .\setup-windows.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "JARVIS" -ForegroundColor White
Write-Host "  http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "  Knowledge tab -> Obsidian -> paste your vault folder -> Connect"
Write-Host "  Ctrl+C to stop."
Write-Host ""

$arguments = @(
    "-m", "uvicorn", "jarvis.api.app:app", "--factory",
    "--host", "127.0.0.1", "--port", "$Port"
)
if ($Reload) { $arguments += "--reload" }

& $venvPython @arguments

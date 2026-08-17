<#
.SYNOPSIS
    Register JARVIS as an MCP server in Claude Desktop.

.DESCRIPTION
    Lets Claude Desktop drive JARVIS, so the reasoning runs on your Claude
    subscription rather than on API credits. JARVIS provides the tools; Claude
    provides the thinking.

    This changes nothing about who decides. Every tool Claude calls goes
    through JARVIS's permission engine exactly as before, and anything needing
    approval stops and waits for you in the JARVIS console at
    http://127.0.0.1:8787. Claude cannot approve on your behalf.

    JARVIS must be running (.\start-jarvis.ps1) for the tools to work.

.PARAMETER Port
    Port the JARVIS daemon is listening on. Default 8787.

.EXAMPLE
    .\connect-claude-desktop.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "No virtual environment found. Run .\setup-windows.ps1 first." -ForegroundColor Red
    exit 1
}

# The token JARVIS already uses. Read from .env rather than generated, because
# a second token would authenticate against nothing.
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "No .env found. Run .\setup-windows.ps1 first." -ForegroundColor Red
    exit 1
}
$tokenLine = Select-String -Path $envFile -Pattern '^JARVIS_API_TOKEN=(.+)$'
if (-not $tokenLine) {
    Write-Host "JARVIS_API_TOKEN is not set in .env." -ForegroundColor Red
    exit 1
}
$token = $tokenLine.Matches[0].Groups[1].Value.Trim()

Write-Host "==> Installing the MCP extra"
& $venvPython -m pip install -e ".[mcp]" --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "  Install failed." -ForegroundColor Red; exit 1 }
Write-Host "    OK" -ForegroundColor Green

$configDir = Join-Path $env:APPDATA "Claude"
$configPath = Join-Path $configDir "claude_desktop_config.json"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

# Merge rather than overwrite: this file is Claude Desktop's, and other MCP
# servers may already be registered in it. Clobbering somebody's existing
# configuration to add ours would be a poor introduction.
if (Test-Path $configPath) {
    Copy-Item $configPath "$configPath.backup" -Force
    Write-Host "==> Backed up existing config to claude_desktop_config.json.backup"
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
} else {
    $config = [PSCustomObject]@{}
}

if (-not $config.PSObject.Properties.Name.Contains("mcpServers")) {
    $config | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([PSCustomObject]@{})
}

$entry = [PSCustomObject]@{
    command = $venvPython
    args    = @("-m", "jarvis.mcp")
    env     = [PSCustomObject]@{
        JARVIS_BASE_URL  = "http://127.0.0.1:$Port"
        JARVIS_API_TOKEN = $token
    }
}

if ($config.mcpServers.PSObject.Properties.Name.Contains("jarvis")) {
    $config.mcpServers.jarvis = $entry
} else {
    $config.mcpServers | Add-Member -NotePropertyName "jarvis" -NotePropertyValue $entry
}

$config | ConvertTo-Json -Depth 10 | Set-Content $configPath -Encoding UTF8
Write-Host "==> Registered JARVIS in Claude Desktop" -ForegroundColor Green
Write-Host "    $configPath"
Write-Host ""
Write-Host "Next:" -ForegroundColor White
Write-Host "  1. Start JARVIS:      .\start-jarvis.ps1"
Write-Host "  2. Restart Claude Desktop completely (quit from the tray, not just close)"
Write-Host "  3. Look for the tools icon in the message box"
Write-Host ""
Write-Host "Approvals still happen in the JARVIS console: http://127.0.0.1:$Port"

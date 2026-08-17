<#
.SYNOPSIS
    Ask Anthropic why JARVIS could not reach it.

.DESCRIPTION
    JARVIS normalises provider failures before they reach the chat panel, so
    "The AI provider had a problem" is deliberately vague — the vendor's text
    stays in the log rather than on screen. This asks the API directly and
    prints what it says, including the list of models this account can
    actually call.

    Never prints the key. It reports the length and last four characters,
    which distinguishes "not loaded" from "loaded but wrong" without putting a
    live credential in output that gets pasted into chat windows.

.EXAMPLE
    .\check-provider.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "No virtual environment found. Run .\setup-windows.ps1 first." -ForegroundColor Red
    exit 1
}

& $venvPython -m jarvis.diagnostics
exit $LASTEXITCODE

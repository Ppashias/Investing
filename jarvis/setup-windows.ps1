<#
.SYNOPSIS
    One-time setup for running JARVIS locally on Windows.

.DESCRIPTION
    Checks the runtime, creates the virtual environment, installs JARVIS,
    creates .env if it is missing, generates an API token, applies the
    database migrations, and (optionally) records an Obsidian vault path.

    Run this once. Afterwards use .\start-jarvis.ps1.

    No secret is stored in this file. The API token is generated on your
    machine and written to .env, which .gitignore already excludes.

.PARAMETER VaultPath
    Your Obsidian vault folder, e.g. C:\Projects\Jarvis. Optional — you can
    also connect from the Obsidian panel in the UI once JARVIS is running.

.PARAMETER SkipTests
    Skip the verification test run.

.EXAMPLE
    .\setup-windows.ps1 -VaultPath C:\Projects\Jarvis
#>

[CmdletBinding()]
param(
    [string]$VaultPath,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Step  { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "    !!  $m" -ForegroundColor Yellow }
function Write-Fail  { param($m) Write-Host "    XX  $m" -ForegroundColor Red }

Write-Host "JARVIS - Windows setup" -ForegroundColor White
Write-Host "Working directory: $PSScriptRoot"

# ── 1. Python ────────────────────────────────────────────────────────────────
# Resolved via the launcher first: `python` on a fresh Windows is often the
# Microsoft Store stub, which exits 9009 and opens the Store instead of running.
Write-Step "Checking Python"

$pythonExe = $null
foreach ($candidate in @(
    @{ Cmd = "py";     Args = @("-3.12") },
    @{ Cmd = "py";     Args = @("-3.11") },
    @{ Cmd = "py";     Args = @("-3") },
    @{ Cmd = "python"; Args = @() }
)) {
    if (-not (Get-Command $candidate.Cmd -ErrorAction SilentlyContinue)) { continue }
    try {
        $version = & $candidate.Cmd @($candidate.Args + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
    } catch { continue }
    if ($LASTEXITCODE -ne 0 -or -not $version) { continue }

    $parts = $version.Trim().Split(".")
    if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
        $pythonExe = @{ Cmd = $candidate.Cmd; Args = $candidate.Args; Version = $version.Trim() }
        break
    }
}

if (-not $pythonExe) {
    Write-Fail "Python 3.11 or newer was not found."
    Write-Host  "    Install it from https://www.python.org/downloads/ and tick"
    Write-Host  "    'Add python.exe to PATH' during installation, then re-run this script."
    exit 1
}
Write-Ok "Python $($pythonExe.Version) via '$($pythonExe.Cmd) $($pythonExe.Args -join ' ')'"

# ── 2. Virtual environment ───────────────────────────────────────────────────
Write-Step "Creating the virtual environment"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Ok ".venv already exists"
} else {
    & $pythonExe.Cmd @($pythonExe.Args + @("-m", "venv", ".venv"))
    if ($LASTEXITCODE -ne 0) { Write-Fail "Could not create .venv"; exit 1 }
    Write-Ok "Created .venv"
}

# ── 3. Dependencies ──────────────────────────────────────────────────────────
Write-Step "Installing JARVIS and its dependencies"
Write-Host  "    (first run downloads a few packages; this takes a minute)"

& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { Write-Fail "Dependency installation failed"; exit 1 }
Write-Ok "Installed"

# Import the application before doing anything else with it. A missing runtime
# dependency shows up here as one clear line, rather than later as an
# ImportError inside a test runner where the useful part is easy to lose.
& $venvPython -c "from jarvis.api.app import create_app" 2>&1 | ForEach-Object { $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Fail "JARVIS could not be imported - see the error above."
    Write-Host  "    This usually means a dependency is missing. Send me that output."
    exit 1
}
Write-Ok "JARVIS imports cleanly"

# ── 4. Configuration ─────────────────────────────────────────────────────────
# .env lives beside this script because that is where the application reads it
# from: config.py resolves REPO_ROOT to the `jarvis` package directory.
Write-Step "Configuration"

$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") $envPath
    Write-Ok "Created .env from .env.example"
} else {
    Write-Ok ".env already exists (leaving it alone)"
}

# Handled line by line, never with a regex over the whole file.
#
# The previous version tested `^\s*JARVIS_API_TOKEN\s*=\s*\S+` against the
# entire text, and `\s` matches newlines: given an empty `JARVIS_API_TOKEN=`
# followed by a blank line and a comment, `=\s*\S+` matched across the line
# break and found the `#`. The script concluded a token was already set,
# skipped generating one, and left the value empty — which is exactly the
# state a real install ended up in. A line is a line; treat it as one.
$envLines = @(Get-Content $envPath)

function Get-EnvValue {
    param([string[]]$Lines, [string]$Key)
    $pattern = "^\s*$([regex]::Escape($Key))\s*=(.*)$"
    foreach ($line in $Lines) {
        if ($line -match $pattern) { return $Matches[1].Trim() }
    }
    return ""
}

function Set-EnvValue {
    param([string[]]$Lines, [string]$Key, [string]$Value)
    # Replaces the key whether it is live or commented out, and appends it if
    # absent — so re-running never produces duplicate keys.
    $pattern = "^\s*#?\s*$([regex]::Escape($Key))\s*="
    $out = New-Object System.Collections.Generic.List[string]
    $written = $false
    foreach ($line in $Lines) {
        if ($line -match $pattern) {
            if (-not $written) { $out.Add("$Key=$Value"); $written = $true }
        } else {
            $out.Add($line)
        }
    }
    if (-not $written) { $out.Add("$Key=$Value") }
    return $out.ToArray()
}

# An API token, generated here rather than shipped. The UI needs it to talk to
# the API; it is written to .env, which .gitignore excludes.
$tokenGenerated = $false
if ([string]::IsNullOrWhiteSpace((Get-EnvValue $envLines "JARVIS_API_TOKEN"))) {
    $token = (& $venvPython -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        Write-Fail "Could not generate an API token"; exit 1
    }
    $envLines = Set-EnvValue $envLines "JARVIS_API_TOKEN" $token
    $tokenGenerated = $true
    Write-Ok "Generated an API token"
} else {
    Write-Ok "API token already set"
}

if ($VaultPath) {
    $resolved = $null
    try { $resolved = (Resolve-Path -LiteralPath $VaultPath -ErrorAction Stop).Path } catch { }

    if (-not $resolved) {
        Write-Warn2 "$VaultPath does not exist yet - not writing it to .env."
        Write-Host  "        Create the vault in Obsidian first, or connect from the UI later."
    } else {
        if (-not (Test-Path (Join-Path $resolved ".obsidian"))) {
            Write-Warn2 "$resolved has no .obsidian folder."
            Write-Host  "        That means Obsidian has never opened it. JARVIS can still read it"
            Write-Host  "        as a Markdown folder, and will say so. Recording it anyway."
        }
        $envLines = Set-EnvValue $envLines "JARVIS_OBSIDIAN_VAULT_PATH" $resolved
        Write-Ok "Vault path recorded: $resolved"
    }
}

# Written without a BOM. Windows PowerShell 5.1's `-Encoding UTF8` emits one,
# and a BOM at the top of .env becomes part of the first key's name for any
# reader that opens the file as plain utf-8 — so the first credential in the
# file, and only that one, silently fails to resolve.
[System.IO.File]::WriteAllLines(
    $envPath, $envLines, (New-Object System.Text.UTF8Encoding($false))
)

# Read it back through the application, so "the token is configured" is
# something observed rather than assumed.
#
# Three details, each of which produced a false failure on the way here:
#   * the log level is forced down, because structlog writes to *stdout* and
#     its lines would otherwise be captured alongside the answer;
#   * the captured output is joined into one string, because `-notmatch`
#     against a PowerShell *array* filters it instead of returning a boolean,
#     and a surviving element is truthy;
#   * the answer is a distinctive sentinel rather than "yes", so it cannot be
#     matched accidentally by unrelated output.
$probe = @"
from jarvis.logging import configure_logging
configure_logging('CRITICAL')
from jarvis.config import get_secret, reset_config_caches
reset_config_caches()
print('TOKEN_READBACK_OK' if get_secret('JARVIS_API_TOKEN') else 'TOKEN_READBACK_FAILED')
"@
$tokenVisible = (& $venvPython -c $probe 2>$null) -join "`n"

if ($tokenVisible -notmatch "TOKEN_READBACK_OK") {
    Write-Fail "The API token was written to .env but JARVIS cannot read it back."
    Write-Host  "    Send me the output of:"
    Write-Host  "      Select-String -Path .env -Pattern JARVIS_API_TOKEN"
    exit 1
}
Write-Ok "JARVIS can read the token from .env"

# ── 5. Database ──────────────────────────────────────────────────────────────
Write-Step "Applying database migrations"

& (Join-Path $PSScriptRoot ".venv\Scripts\alembic.exe") upgrade head
if ($LASTEXITCODE -ne 0) { Write-Fail "Migrations failed"; exit 1 }
Write-Ok "Schema is up to date"

# ── 6. Verify ────────────────────────────────────────────────────────────────
if (-not $SkipTests) {
    Write-Step "Verifying the installation"
    Write-Host  "    (about a minute; some tests skip - they need a Linux X server)"

    # The whole output is captured and only *summarised* on success. On failure
    # it is printed in full: truncating to the last few lines is exactly what
    # loses the traceback that says which import failed.
    $testOutput = & $venvPython -m pytest -q --no-header 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "Some tests did not pass. Full output follows."
        $testOutput | ForEach-Object { Write-Host "    $_" }
        Write-Host ""
        Write-Warn2 "JARVIS may still start. Try .\start-jarvis.ps1, and send me"
        Write-Host  "        the output above if it does not."
    } else {
        ($testOutput | Select-Object -Last 1) | ForEach-Object { Write-Ok $_ }
    }
}

# ── 7. Done ──────────────────────────────────────────────────────────────────
Write-Step "Setup complete"
Write-Host ""
Write-Host "  Start JARVIS with:  " -NoNewline
Write-Host ".\start-jarvis.ps1" -ForegroundColor White
Write-Host "  Then open:          " -NoNewline
Write-Host "http://127.0.0.1:8787" -ForegroundColor White

if ($tokenGenerated) {
    Write-Host ""
    Write-Host "  The page will ask for an access token. Yours is in .env on the"
    Write-Host "  JARVIS_API_TOKEN line. Print it with:"
    Write-Host "    Select-String -Path .env -Pattern JARVIS_API_TOKEN" -ForegroundColor White
}
Write-Host ""

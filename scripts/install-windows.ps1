<#
.SYNOPSIS
Crazy Audiobook Creator - Windows Brain Server Setup

.DESCRIPTION
Prepares the Windows machine (The Brain) for running the Orchestrator, Dashboard, and EPUB extraction pipeline.
#>

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Crazy Audiobook Creator - Windows Brain Server Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# Check if Python is installed
if (-not (Get-Command "python" -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Python is not installed or not in PATH." -ForegroundColor Red
    Write-Host "Please install Python 3.10+ from python.org" -ForegroundColor Yellow
    exit 1
}

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Set-Location $RootDir

# 1. Python Virtual Environment
Write-Host "[1/4] Setting up Python virtual environment..." -ForegroundColor Green
if (-not (Test-Path "venv")) {
    python -m venv venv
}
& .\venv\Scripts\Activate.ps1

# 2. Python Packages
Write-Host "[2/4] Installing Python dependencies..." -ForegroundColor Green
python -m pip install --upgrade pip
pip install -r brain/requirements.txt

# 3. Check for Ollama
Write-Host "[3/4] Checking for Ollama..." -ForegroundColor Green
if (-not (Get-Command "ollama" -ErrorAction SilentlyContinue)) {
    Write-Host "WARNING: Ollama not found in PATH." -ForegroundColor Yellow
    Write-Host "Please install Ollama from https://ollama.com to use the LLM Director." -ForegroundColor Yellow
} else {
    Write-Host "Ollama found. Pulling required models..." -ForegroundColor Cyan
    ollama pull qwen2.5:7b
}

Write-Host "[4/4] Granting inheritable write access on the artifact trees..." -ForegroundColor Green

# `os.replace` needs the DELETE right on the file it overwrites. A process that
# ever runs elevated -- a task registered with -LogonType S4U, or a launch from
# an admin shell -- creates files owned by BUILTIN\Administrators, and the
# normal unelevated service then cannot replace them. The failure appears much
# later, somewhere unrelated, as '[WinError 5] Access is denied' on a rename.
#
# An inheritable (OI)(CI) grant for this user makes that harmless: ownership
# may be wrong, but replacement still works. Applied to the trees the app
# writes to; the repo itself is not touched.
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
foreach ($relative in @("brain\projects", "workspace", "voice_library", "voice")) {
    $target = Join-Path $RootDir $relative
    if (-not (Test-Path -LiteralPath $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
    & icacls.exe $target /grant "${currentUser}:(OI)(CI)F" /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning (
            "Could not grant inheritable write access on '$relative'. If the app " +
            "is ever run elevated, a later unelevated run may fail with WinError 5. " +
            "See Troubleshooting in docs/setup-windows.md."
        )
    }
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Setup complete! " -ForegroundColor Green
Write-Host ""
Write-Host " To run the brain dashboard:"
Write-Host "   .\venv\Scripts\Activate.ps1"
Write-Host "   python -m brain.dashboard.api.main"
Write-Host "============================================================" -ForegroundColor Cyan

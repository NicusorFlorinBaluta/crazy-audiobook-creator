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
Write-Host "[1/3] Setting up Python virtual environment..." -ForegroundColor Green
if (-not (Test-Path "venv")) {
    python -m venv venv
}
& .\venv\Scripts\Activate.ps1

# 2. Python Packages
Write-Host "[2/3] Installing Python dependencies..." -ForegroundColor Green
python -m pip install --upgrade pip
pip install -r brain/requirements.txt

# 3. Check for Ollama
Write-Host "[3/3] Checking for Ollama..." -ForegroundColor Green
if (-not (Get-Command "ollama" -ErrorAction SilentlyContinue)) {
    Write-Host "WARNING: Ollama not found in PATH." -ForegroundColor Yellow
    Write-Host "Please install Ollama from https://ollama.com to use the LLM Director." -ForegroundColor Yellow
} else {
    Write-Host "Ollama found. Pulling required models..." -ForegroundColor Cyan
    ollama pull qwen2.5:7b
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " Setup complete! " -ForegroundColor Green
Write-Host ""
Write-Host " To run the brain dashboard:"
Write-Host "   .\venv\Scripts\Activate.ps1"
Write-Host "   python -m brain.dashboard.api.main"
Write-Host "============================================================" -ForegroundColor Cyan

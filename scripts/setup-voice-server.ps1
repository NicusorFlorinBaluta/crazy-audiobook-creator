# Setup Voice Server on Windows for AMD 7900 XTX (ROCm)
param (
    [string]$VenvPath = "E:\PyTorch env\my_venv",
    [switch]$DryRun
)

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Audiobook Creator -- Local Voice Server Windows Setup" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# Step 1: Verify Python venv
$PythonExe = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Write-Error "AMD PyTorch venv not found at '$VenvPath'. Please create it or set -VenvPath."
    exit 1
}
Write-Host "[OK] Found AMD PyTorch venv: $VenvPath" -ForegroundColor Green
if ($DryRun) {
    Write-Host "[DRY RUN] No packages will be installed or removed." -ForegroundColor Cyan
    & $PythonExe "$PSScriptRoot\runtime_preflight.py" --pip-check
    exit $LASTEXITCODE
}

# Step 2: Remove the retired Parler package from this Qwen environment
Write-Host ""
Write-Host "[1/4] Checking for retired Parler dependencies..." -ForegroundColor Yellow
& $PythonExe -m pip show parler-tts *> $null
if ($LASTEXITCODE -eq 0) {
    & $PythonExe -m pip uninstall -y parler-tts
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Could not remove parler-tts from the Qwen environment."
        exit $LASTEXITCODE
    }
    Write-Host "[OK] Removed incompatible parler-tts package." -ForegroundColor Green
} else {
    Write-Host "[OK] No incompatible parler-tts package is installed." -ForegroundColor Green
}

# Step 3: Install the supported Qwen voice-server dependencies
Write-Host ""
Write-Host "[2/4] Installing voice server dependencies into AMD venv..." -ForegroundColor Yellow
$RequirementsPath = Join-Path $PSScriptRoot "..\voice\requirements.txt"
$ConstraintsPath = Join-Path $PSScriptRoot "..\voice\constraints-windows-rocm-tested.txt"
& $PythonExe -m pip install -r $RequirementsPath -c $ConstraintsPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "Pip install failed with exit code $LASTEXITCODE. Please check your network or python venv."
    exit $LASTEXITCODE
}

# Step 4: Verify the resolved environment
Write-Host ""
Write-Host "[3/4] Checking Python dependency consistency..." -ForegroundColor Yellow
& $PythonExe -m pip check
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python dependency consistency check failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

# Step 5: Verify FFmpeg
Write-Host ""
Write-Host "[4/4] Checking FFmpeg installation..." -ForegroundColor Yellow
$FFmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($FFmpegCmd) {
    Write-Host "[OK] FFmpeg is available at: $($FFmpegCmd.Source)" -ForegroundColor Green
} else {
    Write-Warning "FFmpeg was not found in PATH! M4B export requires FFmpeg. Please install FFmpeg and add it to PATH."
}

Write-Host ""
Write-Host "Writing read-only runtime compatibility report..." -ForegroundColor Yellow
$ReportPath = Join-Path $PSScriptRoot "..\runtime-environment.json"
& $PythonExe "$PSScriptRoot\runtime_preflight.py" --pip-check --output $ReportPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "Runtime compatibility preflight failed. Review $ReportPath."
    exit $LASTEXITCODE
}
Write-Host "[OK] Runtime report: $ReportPath" -ForegroundColor Green

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Local Voice Server Setup Complete!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

# Setup Voice Server on Windows for AMD 7900 XTX (ROCm)
param (
    [string]$VenvPath = "E:\PyTorch env\my_venv"
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

# Step 2: Install voice server dependencies
Write-Host ""
Write-Host "[1/3] Installing voice server dependencies into AMD venv..." -ForegroundColor Yellow
$env:GIT_CLONE_PROTECTION_ACTIVE = "false"
& $PythonExe -m pip install git+https://github.com/huggingface/parler-tts.git soundfile openai-whisper plyer requests python-multipart ebooklib beautifulsoup4

# Step 3: Apply audiotools ROCm patch if needed
Write-Host ""
Write-Host "[2/3] Checking audiotools ROCm patch..." -ForegroundColor Yellow
$DecoratorPath = Join-Path $VenvPath "Lib\site-packages\audiotools\ml\decorators.py"
if (Test-Path $DecoratorPath) {
    $content = Get-Content $DecoratorPath -Raw
    if ($content -match "op: dist\.ReduceOp = dist\.ReduceOp\.AVG") {
        Write-Host "Applying ReduceOp patch to $DecoratorPath..." -ForegroundColor Yellow
        $content = $content -replace "op: dist\.ReduceOp = dist\.ReduceOp\.AVG", "op: dist.ReduceOp = None"
        Set-Content -Path $DecoratorPath -Value $content -NoNewline
        Write-Host "[OK] Audiotools patch applied successfully!" -ForegroundColor Green
    } else {
        Write-Host "[OK] Audiotools patch already applied or not required." -ForegroundColor Green
    }
} else {
    Write-Host "[!] audiotools/ml/decorators.py not found in venv (will be checked when audiotools is loaded)." -ForegroundColor Yellow
}

# Step 4: Verify FFmpeg
Write-Host ""
Write-Host "[3/3] Checking FFmpeg installation..." -ForegroundColor Yellow
$FFmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($FFmpegCmd) {
    Write-Host "[OK] FFmpeg is available at: $($FFmpegCmd.Source)" -ForegroundColor Green
} else {
    Write-Warning "FFmpeg was not found in PATH! M4B export requires FFmpeg. Please install FFmpeg and add it to PATH."
}

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Local Voice Server Setup Complete!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan

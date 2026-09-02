[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [switch]$NoSupervise,
    [int]$CheckIntervalSeconds = 15,
    [int]$MaxFailedChecks = 5
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentFile = Join-Path $projectRoot ".env"

if (Test-Path -LiteralPath $environmentFile) {
    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed -split "=", 2
        if ($parts.Count -ne 2) {
            continue
        }
        [System.Environment]::SetEnvironmentVariable(
            $parts[0].Trim(),
            $parts[1].Trim(),
            "Process"
        )
    }
}

if (-not $env:CRAZY_AUDIOBOOK_DASHBOARD_TOKEN) {
    throw "CRAZY_AUDIOBOOK_DASHBOARD_TOKEN is required. Copy .env.example to .env and set a long random token."
}

function Test-PythonLauncher {
    param([string]$Candidate)
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) {
        return $false
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $Candidate -c "import sys" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

$configuredFallback = $env:CRAZY_AUDIOBOOK_DASHBOARD_PYTHON
$projectVenvPython = Join-Path $projectRoot "venv\Scripts\python.exe"
$voiceVenvPython = "E:\PYTORC~1\my_venv\Scripts\python.exe"
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue

$candidates = @(
    $PythonExecutable,
    $projectVenvPython,
    $configuredFallback,
    $voiceVenvPython,
    $(if ($pythonCommand) { $pythonCommand.Source })
) | Where-Object { $_ } | Select-Object -Unique

$workingPython = $candidates |
    Where-Object { Test-PythonLauncher $_ } |
    Select-Object -First 1
if (-not $workingPython) {
    throw "No working Python interpreter was found."
}
$PythonExecutable = $workingPython

$pythonPaths = @($projectRoot)
if ($env:PYTHONPATH) {
    $pythonPaths += $env:PYTHONPATH
}
$env:PYTHONPATH = (
    $pythonPaths |
    Where-Object { $_ } |
    Select-Object -Unique
) -join [IO.Path]::PathSeparator

function Stop-StalePort8000Process {
    Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Set-Location -LiteralPath $projectRoot

if ($NoSupervise) {
    Stop-StalePort8000Process
    & $PythonExecutable -m brain.dashboard.api.main
    exit $LASTEXITCODE
}

# ==============================================================================
# Self-Healing Supervisor Watchdog
# ==============================================================================
$shutdownSentinel = Join-Path $projectRoot "brain\projects\.dashboard_shutdown"
if (Test-Path -LiteralPath $shutdownSentinel) {
    Remove-Item -LiteralPath $shutdownSentinel -Force -ErrorAction SilentlyContinue
}

Write-Host "Starting Brain Dashboard under Self-Healing Supervisor..." -ForegroundColor Cyan

while ($true) {
    Stop-StalePort8000Process
    
    $proc = Start-Process -FilePath $PythonExecutable `
        -ArgumentList "-m brain.dashboard.api.main" `
        -WorkingDirectory $projectRoot `
        -PassThru `
        -NoNewWindow

    if (-not $proc) {
        Write-Error "Failed to start Python dashboard process."
        exit 1
    }

    $failCount = 0
    $userShutdown = $false

    # Initial warm-up grace period
    Start-Sleep -Seconds 3

    while ($true) {
        if ($proc.HasExited) {
            $exitCode = $proc.ExitCode
            if ($exitCode -eq 0 -or (Test-Path -LiteralPath $shutdownSentinel)) {
                Write-Host "Dashboard received intentional shutdown (ExitCode: $exitCode). Exiting supervisor." -ForegroundColor Green
                if (Test-Path -LiteralPath $shutdownSentinel) {
                    Remove-Item -LiteralPath $shutdownSentinel -Force -ErrorAction SilentlyContinue
                }
                exit 0
            }
            Write-Warning "Dashboard process exited unexpectedly (ExitCode: $exitCode). Auto-recovering in 2s..."
            Start-Sleep -Seconds 2
            break
        }

        if (Test-Path -LiteralPath $shutdownSentinel) {
            Write-Host "Shutdown sentinel detected (user requested shutdown via UI). Waiting for process to exit..." -ForegroundColor Green
            $proc.WaitForExit(5000)
            Remove-Item -LiteralPath $shutdownSentinel -Force -ErrorAction SilentlyContinue
            exit 0
        }

        # Health probe
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                $failCount = 0
            } else {
                $failCount++
            }
        }
        catch {
            $failCount++
        }

        if ($failCount -ge $MaxFailedChecks) {
            Write-Warning "Dashboard socket became unresponsive ($failCount consecutive probe failures). Triggering auto-recovery..."
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
            break
        }

        Start-Sleep -Seconds $CheckIntervalSeconds
    }
}

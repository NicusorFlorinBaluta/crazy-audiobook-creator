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
    <#
        .SYNOPSIS
        Free port 8000, but only from something that is actually stale.

        .DESCRIPTION
        This used to be an unconditional `Stop-Process -Force` on whatever
        owned port 8000, run at the top of every supervisor iteration and at
        launcher start. Any second launch of the task -- including Task
        Scheduler's own restart-on-failure, which fires without anyone asking
        -- therefore hard-killed a completely healthy dashboard. On 2026-09-04
        a task relaunch landed three minutes into a book and took the run with
        it; the pipeline logged "Interrupted by user" for an interruption no
        user requested.

        A listener that answers /api/health is not stale, and a listener that
        reports a running pipeline must not be disturbed at all. Only after
        those two checks, and only after a graceful shutdown has been given a
        chance, does force remain on the table -- which is what keeps the
        self-healing behaviour for the case it was written for: a wedged
        process that holds the port and answers nothing.
    #>
    param([int]$GraceSeconds = 20)

    $owners = @(
        Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($owners.Count -eq 0) {
        return $true
    }

    $health = $null
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 3
    }
    catch {
        $health = $null
    }

    if ($health -and $health.pipeline_running) {
        Write-Warning (
            "Port 8000 is held by a healthy dashboard with a pipeline running. " +
            "Refusing to kill it -- stop the run first, or use " +
            "scripts/restart_dashboard.ps1, which shuts down cooperatively."
        )
        return $false
    }

    if ($health) {
        Write-Host "Port 8000 is held by a healthy idle dashboard; asking it to shut down." -ForegroundColor Yellow
        try {
            Invoke-RestMethod `
                -Uri "http://127.0.0.1:8000/api/system/shutdown" `
                -Method Post `
                -ContentType "application/json" `
                -Body "{}" `
                -TimeoutSec 10 | Out-Null
        }
        catch {
            Write-Warning "Cooperative shutdown was refused: $($_.Exception.Message)"
        }

        $deadline = (Get-Date).AddSeconds($GraceSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $still = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
            if (-not $still) {
                return $true
            }
        }
        Write-Warning "The dashboard did not release port 8000 within ${GraceSeconds}s; forcing."
    }
    else {
        Write-Warning "Port 8000 is held by a process that does not answer /api/health; forcing."
    }

    foreach ($owningPid in $owners) {
        Stop-Process -Id $owningPid -Force -ErrorAction SilentlyContinue
    }
    return $true
}

Set-Location -LiteralPath $projectRoot

if ($NoSupervise) {
    if (-not (Stop-StalePort8000Process)) {
        Write-Error "A dashboard is already serving port 8000 with a pipeline running."
        exit 1
    }
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
    if (-not (Stop-StalePort8000Process)) {
        # A healthy dashboard is mid-run. Starting a second one cannot help --
        # it would only fail to bind, or worse, win the port and kill the book.
        # Task Scheduler's restart-on-failure reaches here without anyone
        # asking for a restart, so backing off is the whole point.
        Write-Warning "Deferring to the dashboard that is already running a pipeline. Exiting supervisor."
        exit 0
    }

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

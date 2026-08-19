[CmdletBinding()]
param(
    [string]$PythonExecutable = ""
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

Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Set-Location -LiteralPath $projectRoot
& $PythonExecutable -m brain.dashboard.api.main
exit $LASTEXITCODE

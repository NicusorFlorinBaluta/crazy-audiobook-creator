[CmdletBinding()]
param(
    [string]$PythonExecutable = "E:\PYTORC~1\my_venv\Scripts\python.exe"
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

$venvRoot = Split-Path -Parent (Split-Path -Parent $PythonExecutable)
$configuredFallback = $env:CRAZY_AUDIOBOOK_DASHBOARD_PYTHON
$bundledFallback = Join-Path $env:USERPROFILE `
    ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$candidates = @(
    $PythonExecutable,
    $configuredFallback,
    $(if ($pythonCommand) { $pythonCommand.Source }),
    $bundledFallback
) | Where-Object { $_ } | Select-Object -Unique

$workingPython = $candidates |
    Where-Object { Test-PythonLauncher $_ } |
    Select-Object -First 1
if (-not $workingPython) {
    throw (
        "No working Python interpreter was found. Set " +
        "CRAZY_AUDIOBOOK_DASHBOARD_PYTHON in .env to a Python 3.12 executable."
    )
}
$PythonExecutable = $workingPython

# A portable fallback interpreter can reuse the already-installed packages in
# the configured Voice virtual environment. Keep the repository first so local
# application modules always resolve to this checkout.
$pythonPaths = @($projectRoot)
$sitePackages = Join-Path $venvRoot "Lib\site-packages"
if (Test-Path -LiteralPath $sitePackages) {
    $pythonPaths += $sitePackages
}
if ($env:PYTHONPATH) {
    $pythonPaths += $env:PYTHONPATH
}
$env:PYTHONPATH = (
    $pythonPaths |
    Where-Object { $_ } |
    Select-Object -Unique
) -join [IO.Path]::PathSeparator

Set-Location -LiteralPath $projectRoot
& $PythonExecutable -m brain.dashboard.api.main
exit $LASTEXITCODE

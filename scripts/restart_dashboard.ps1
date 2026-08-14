[CmdletBinding()]
param(
    [string]$TaskName = "Crazy Audiobook Dashboard",
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [int]$ShutdownTimeoutSeconds = 45,
    [int]$StartupTimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentFile = Join-Path $projectRoot ".env"
$dashboardHeaders = @{}

if (Test-Path -LiteralPath $environmentFile) {
    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed -split "=", 2
        if ($parts.Count -eq 2 -and $parts[0].Trim() -eq "CRAZY_AUDIOBOOK_DASHBOARD_TOKEN") {
            $dashboardHeaders["X-API-Token"] = $parts[1].Trim()
            break
        }
    }
}

function Test-DashboardOnline {
    try {
        Invoke-RestMethod `
            -Uri "$BaseUrl/api/projects" `
            -Method Get `
            -Headers $dashboardHeaders `
            -TimeoutSec 2 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

$listener = Get-NetTCPConnection `
    -LocalPort 8000 `
    -State Listen `
    -ErrorAction SilentlyContinue

if ($listener) {
    try {
        $response = Invoke-RestMethod `
            -Uri "$BaseUrl/api/system/shutdown" `
            -Method Post `
            -Headers $dashboardHeaders `
            -ContentType "application/json" `
            -Body "{}" `
            -TimeoutSec 10
        Write-Host (
            "Dashboard shutdown accepted for PID {0}." -f $response.pid
        )
    }
    catch {
        throw (
            "The running dashboard does not support controlled shutdown yet. " +
            "Stop the existing port-8000 process once, start the updated task, " +
            "then this helper can manage future restarts without intervention. " +
            "Original error: $($_.Exception.Message)"
        )
    }

}

if ($listener) {
    $deadline = (Get-Date).AddSeconds($ShutdownTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $listener = Get-NetTCPConnection `
            -LocalPort 8000 `
            -State Listen `
            -ErrorAction SilentlyContinue
    } while ($listener -and (Get-Date) -lt $deadline)

    if ($listener) {
        throw "Dashboard did not release port 8000 within the shutdown timeout."
    }
}

# The API port can close just before Task Scheduler marks the launcher action
# complete. With MultipleInstances=IgnoreNew, starting during that short window
# reports success but silently drops the replacement instance.
$taskDeadline = (Get-Date).AddSeconds($ShutdownTimeoutSeconds)
do {
    $scheduledTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($scheduledTask.State -ne "Running") {
        break
    }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $taskDeadline)

if ($scheduledTask.State -eq "Running") {
    throw "Scheduled task '$TaskName' did not stop within the shutdown timeout."
}

schtasks.exe /Run /TN $TaskName | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not run the registered task '$TaskName'."
}

$deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
do {
    Start-Sleep -Milliseconds 750
    if (Test-DashboardOnline) {
        Write-Host "Dashboard is ready at $BaseUrl."
        exit 0
    }
} while ((Get-Date) -lt $deadline)

throw "Dashboard did not become ready within the startup timeout."

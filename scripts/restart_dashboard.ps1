[CmdletBinding()]
param(
    [string]$TaskName = "Crazy Audiobook Dashboard",
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [int]$ShutdownTimeoutSeconds = 45,
    [int]$StartupTimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"

function Test-DashboardOnline {
    try {
        Invoke-RestMethod `
            -Uri "$BaseUrl/api/projects" `
            -Method Get `
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

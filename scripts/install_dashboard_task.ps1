[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Crazy Audiobook Dashboard",
    [string]$RestartTaskName = "Crazy Audiobook Dashboard Restart"
)

$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "start_dashboard.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Dashboard launcher not found at '$launcher'."
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentFile = Join-Path $projectRoot ".env"
if (-not (Test-Path -LiteralPath $environmentFile)) {
    throw "Create '$environmentFile' from .env.example before registering the task."
}

$escapedLauncher = $launcher.Replace('"', '""')
$arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$escapedLauncher`""
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $projectRoot
$restartLauncher = Join-Path $PSScriptRoot "restart_dashboard.ps1"
$escapedRestartLauncher = $restartLauncher.Replace('"', '""')
$restartArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$escapedRestartLauncher`" -TaskName `"$TaskName`""
$restartAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $restartArguments `
    -WorkingDirectory $projectRoot
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U `
    -RunLevel Limited
$restartPrincipal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register on-demand audiobook dashboard task")) {
    $existingDashboardTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if (-not $existingDashboardTask) {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Settings $settings `
            -Principal $principal `
            -Description "Runs the Crazy Audiobook Creator dashboard headlessly on demand." | Out-Null
    }
    else {
        Write-Output "Preserved existing '$TaskName' task."
    }
    $restartSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask `
        -TaskName $RestartTaskName `
        -Action $restartAction `
        -Settings $restartSettings `
        -Principal $restartPrincipal `
        -Description "Safely restarts the Crazy Audiobook Creator dashboard." `
        -Force | Out-Null
    Write-Output "Ensured '$TaskName' and '$RestartTaskName' are available."
}

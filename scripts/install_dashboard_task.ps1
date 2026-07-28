[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Crazy Audiobook Dashboard"
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

if ($PSCmdlet.ShouldProcess($TaskName, "Register on-demand audiobook dashboard task")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Settings $settings `
        -Principal $principal `
        -Description "Runs the Crazy Audiobook Creator dashboard headlessly on demand." `
        -Force | Out-Null
    Write-Output "Registered '$TaskName'. Test it with: schtasks.exe /Run /TN `"$TaskName`""
}

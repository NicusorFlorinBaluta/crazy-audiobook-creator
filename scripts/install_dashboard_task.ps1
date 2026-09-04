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
# The dashboard task runs headless and must NOT hold an elevated token.
#
# It used to use -LogonType S4U. For an account in Administrators that returns
# a full token, and -RunLevel Limited does not filter it: UAC filtering applies
# to interactive-style logons, not to service-for-user. Everything the run
# wrote then landed owned by BUILTIN\Administrators, and a later unelevated
# run could not replace those files -- '[WinError 5] Access is denied' on a
# rename, surfacing somewhere unrelated. Measured 2026-09-04 with two throwaway
# tasks differing only in logon type: S4U elevated, Interactive not.
#
# Password gives the same filtered token as Interactive while still running
# with nobody logged on, which is the one thing S4U was chosen for.
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Password `
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
        # Prompted for, passed straight through, never stored by this script.
        # Windows keeps it in the Task Scheduler credential vault.
        $credential = Get-Credential -UserName $currentUser `
            -Message "Password for $currentUser, so the dashboard task can run headless without an elevated token"
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Settings $settings `
            -Principal $principal `
            -User $currentUser `
            -Password $credential.GetNetworkCredential().Password `
            -Description "Runs the Crazy Audiobook Creator dashboard headlessly on demand." | Out-Null
    }
    else {
        Write-Output "Preserved existing '$TaskName' task."
        if ($existingDashboardTask.Principal.LogonType -eq "S4U") {
            Write-Warning (
                "'$TaskName' is registered with -LogonType S4U. On an account in " +
                "Administrators that runs the dashboard ELEVATED despite RunLevel " +
                "Limited, and every artifact it writes becomes owned by " +
                "BUILTIN\Administrators. Unregister the task and re-run this " +
                "script to move it to -LogonType Password."
            )
        }
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

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Crazy Audiobook Dashboard",
    [string]$RestartTaskName = "Crazy Audiobook Dashboard Restart",

    # Auto picks Password for a local account and Interactive for a Microsoft
    # account, which cannot perform the batch logon Password requires. Both
    # yield the unelevated token that matters; see the comment by $logonType.
    [ValidateSet("Auto", "Interactive", "Password")]
    [string]$LogonType = "Auto"
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
# Choosing the logon type is the whole point of this block.
#
# It used to be S4U. For an account in Administrators that returns a *full*
# token, and -RunLevel Limited does not filter it -- UAC filtering applies to
# interactive-style logons, not to service-for-user. Everything the dashboard
# wrote then landed owned by BUILTIN\Administrators, and a later unelevated
# run could not replace those files: '[WinError 5] Access is denied' on a
# rename, surfacing somewhere unrelated. Measured 2026-09-04 with two throwaway
# tasks differing only in logon type: S4U elevated, Interactive not.
#
# Password would keep the headless behaviour S4U was chosen for, but it needs a
# batch logon, and a Microsoft account cannot do one under its local
# MACHINE\user name -- Register-ScheduledTask fails with 0x8007052E however
# correct the password is. So the account type decides:
#
#   local account     -> Password     (headless, prompts once)
#   Microsoft account -> Interactive  (no credential, needs a logged-on session)
#
# Both produce the unelevated token. Only the headless property differs.
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$shortName = $currentUser.Split('\')[-1]
$resolvedLogon = $LogonType
if ($resolvedLogon -eq "Auto") {
    $localAccount = Get-LocalUser -Name $shortName -ErrorAction SilentlyContinue
    if ($localAccount -and $localAccount.PrincipalSource -eq "MicrosoftAccount") {
        $resolvedLogon = "Interactive"
        Write-Host (
            "'$currentUser' is a Microsoft account, which cannot perform the batch " +
            "logon that -LogonType Password requires. Registering Interactive instead: " +
            "unelevated as intended, but the task will not start unless someone is " +
            "logged on. Pass -LogonType Password to override."
        ) -ForegroundColor Yellow
    }
    else {
        $resolvedLogon = "Password"
    }
}
$restartPrincipal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register on-demand audiobook dashboard task")) {
    $existingDashboardTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if (-not $existingDashboardTask) {
        $description = "Runs the Crazy Audiobook Creator dashboard on demand, unelevated."
        if ($resolvedLogon -eq "Password") {
            # Prompted for, passed straight through, never stored by this
            # script. Windows keeps it in the Task Scheduler credential vault.
            $credential = Get-Credential -UserName $currentUser `
                -Message "Password for $currentUser, so the dashboard task can run headless without an elevated token"
            if (-not $credential) {
                throw "Registration cancelled: -LogonType Password needs a password."
            }
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Settings $settings `
                -User $currentUser `
                -Password $credential.GetNetworkCredential().Password `
                -RunLevel Limited `
                -Description $description | Out-Null
        }
        else {
            $principal = New-ScheduledTaskPrincipal `
                -UserId $currentUser `
                -LogonType Interactive `
                -RunLevel Limited
            Register-ScheduledTask `
                -TaskName $TaskName `
                -Action $action `
                -Settings $settings `
                -Principal $principal `
                -Description $description | Out-Null
        }
        Write-Output "Registered '$TaskName' with -LogonType $resolvedLogon."
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

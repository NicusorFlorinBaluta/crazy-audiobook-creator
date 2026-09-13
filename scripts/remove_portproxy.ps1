[CmdletBinding()]
param(
    [int]$ListenPort = 8000,
    [string]$ListenAddress = "0.0.0.0"
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host "Elevating permissions to remove Windows PortProxy rule..." -ForegroundColor Yellow
    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-ListenPort", $ListenPort,
        "-ListenAddress", "`"$ListenAddress`""
    )
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -Wait
    exit $LASTEXITCODE
}

Write-Host "Removing Windows PortProxy: $ListenAddress`:$ListenPort" -ForegroundColor Cyan
netsh interface portproxy delete v4tov4 listenport=$ListenPort listenaddress=$ListenAddress -ErrorAction SilentlyContinue

Write-Host "`nCurrent Active PortProxy Rules:" -ForegroundColor Green
netsh interface portproxy show all
Write-Host "`nPortProxy rule removed." -ForegroundColor Green

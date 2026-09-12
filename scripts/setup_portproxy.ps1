[CmdletBinding()]
param(
    [int]$ListenPort = 8000,
    [string]$ListenAddress = "0.0.0.0",
    [int]$ConnectPort = 8000,
    [string]$ConnectAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host "Elevating permissions to configure Windows PortProxy..." -ForegroundColor Yellow
    $argList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-ListenPort", $ListenPort,
        "-ListenAddress", "`"$ListenAddress`"",
        "-ConnectPort", $ConnectPort,
        "-ConnectAddress", "`"$ConnectAddress`""
    )
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -Wait
    exit $LASTEXITCODE
}

Write-Host "Configuring Windows PortProxy: $ListenAddress`:$ListenPort -> $ConnectAddress`:$ConnectPort" -ForegroundColor Cyan
netsh interface portproxy add v4tov4 listenport=$ListenPort listenaddress=$ListenAddress connectport=$ConnectPort connectaddress=$ConnectAddress

Write-Host "`nCurrent Active PortProxy Rules:" -ForegroundColor Green
netsh interface portproxy show all
Write-Host "`nPortProxy configuration complete. External LAN connections to port $ListenPort will now be safely routed to loopback $ConnectAddress." -ForegroundColor Green

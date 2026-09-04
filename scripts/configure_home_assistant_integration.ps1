[CmdletBinding()]
param(
    [string]$HomeAssistantRepo = "E:\Projects\crazy-ha"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$projectRoot = Split-Path -Parent $PSScriptRoot
$appEnvironmentPath = Join-Path $projectRoot ".env"
$haSecretsPath = Join-Path $HomeAssistantRepo "secrets.yaml"
$npmConfigPath = Join-Path $HomeAssistantRepo "npm-audiobook-location.conf"

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match ("^\s*" + [regex]::Escape($Key) + "\s*=\s*(?<value>.*)\s*$")) {
            return $Matches.value.Trim()
        }
    }
    return ""
}

function Set-DotEnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $lines = if (Test-Path -LiteralPath $Path) {
        [System.IO.File]::ReadAllLines($Path)
    } else {
        @()
    }
    $output = New-Object System.Collections.Generic.List[string]
    $written = $false
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    foreach ($line in $lines) {
        if ($line -match $pattern) {
            if (-not $written) {
                $output.Add("$Key=$Value")
                $written = $true
            }
            continue
        }
        $output.Add($line)
    }
    if (-not $written) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1]) {
            $output.Add("")
        }
        $output.Add("$Key=$Value")
    }
    [System.IO.File]::WriteAllText(
        $Path,
        (($output -join [Environment]::NewLine) + [Environment]::NewLine),
        $utf8NoBom
    )
}

function Get-YamlScalar {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match ("^\s*" + [regex]::Escape($Key) + "\s*:\s*(?<value>.*?)\s*$")) {
            $value = $Matches.value.Trim()
            if (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            ) {
                return $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return ""
}

function Set-YamlScalar {
    param([string]$Path, [string]$Key, [string]$Value)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required Home Assistant secrets file not found: $Path"
    }
    if ($Value.Contains('"')) {
        throw "The value for '$Key' cannot contain a double quote."
    }
    $lines = [System.IO.File]::ReadAllLines($Path)
    $output = New-Object System.Collections.Generic.List[string]
    $written = $false
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*:"
    foreach ($line in $lines) {
        if ($line -match $pattern) {
            if (-not $written) {
                $output.Add("${Key}: `"$Value`"")
                $written = $true
            }
            continue
        }
        $output.Add($line)
    }
    if (-not $written) {
        if ($output.Count -gt 0 -and $output[$output.Count - 1]) {
            $output.Add("")
        }
        $output.Add("${Key}: `"$Value`"")
    }
    [System.IO.File]::WriteAllText(
        $Path,
        (($output -join [Environment]::NewLine) + [Environment]::NewLine),
        $utf8NoBom
    )
}

function New-DashboardToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Get-CrazyPcAddress {
    param([string]$HomeAssistantAddress)
    $haOctets = $HomeAssistantAddress -split "\."
    $preferredPrefix = if ($haOctets.Count -eq 4) {
        ($haOctets[0..2] -join ".") + "."
    } else {
        ""
    }

    $candidates = @()
    foreach (
        $adapter in
        [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()
    ) {
        if (
            $adapter.OperationalStatus -ne
                [System.Net.NetworkInformation.OperationalStatus]::Up -or
            $adapter.NetworkInterfaceType -eq
                [System.Net.NetworkInformation.NetworkInterfaceType]::Loopback -or
            $adapter.NetworkInterfaceType -eq
                [System.Net.NetworkInformation.NetworkInterfaceType]::Tunnel
        ) {
            continue
        }
        $properties = $adapter.GetIPProperties()
        $hasGateway = @($properties.GatewayAddresses) |
            Where-Object {
                $_.Address.AddressFamily -eq
                    [System.Net.Sockets.AddressFamily]::InterNetwork -and
                $_.Address.ToString() -ne "0.0.0.0"
            } |
            Select-Object -First 1
        if (-not $hasGateway) {
            continue
        }
        foreach ($address in @($properties.UnicastAddresses)) {
            if (
                $address.Address.AddressFamily -ne
                [System.Net.Sockets.AddressFamily]::InterNetwork
            ) {
                continue
            }
            $addressText = $address.Address.ToString()
            if (
                $addressText -like "127.*" -or
                $addressText -like "169.254.*"
            ) {
                continue
            }
            $candidates += [pscustomobject]@{
                Address = $addressText
                PreferredSubnet = [int](
                    $preferredPrefix -and
                    $addressText.StartsWith($preferredPrefix)
                )
                Ethernet = [int](
                    $adapter.NetworkInterfaceType -eq
                    [System.Net.NetworkInformation.NetworkInterfaceType]::Ethernet
                )
                Physical = [int]($adapter.Description -notmatch "Virtual|VPN")
            }
        }
    }
    $selected = $candidates |
        Sort-Object `
            @{Expression = "PreferredSubnet"; Descending = $true},
            @{Expression = "Ethernet"; Descending = $true},
            @{Expression = "Physical"; Descending = $true},
            @{Expression = "Address"; Descending = $false} |
        Select-Object -First 1
    if (-not $selected) {
        throw "Could not detect a routed IPv4 address for Crazy-PC."
    }
    return $selected.Address
}

if (-not (Test-Path -LiteralPath $haSecretsPath)) {
    throw "Home Assistant secrets.yaml was not found at '$haSecretsPath'."
}

$haExternalUrl = Get-YamlScalar $haSecretsPath "ha_external_url"
$haAddress = Get-YamlScalar $haSecretsPath "ha_ip"
$proxyAddress = Get-YamlScalar $haSecretsPath "nginx_proxy_ip"
if (-not $haExternalUrl -or -not $haAddress -or -not $proxyAddress) {
    throw "ha_external_url, ha_ip, and nginx_proxy_ip must exist in HA secrets.yaml."
}
if ($haExternalUrl -notmatch "^https://") {
    throw "ha_external_url must use HTTPS before the app can be embedded remotely."
}

$crazyPcAddress = Get-CrazyPcAddress $haAddress
$appToken = Get-DotEnvValue $appEnvironmentPath "CRAZY_AUDIOBOOK_DASHBOARD_TOKEN"
$haToken = Get-YamlScalar $haSecretsPath "audiobook_api_token"
if ($appToken -and $haToken -and $appToken -ne $haToken) {
    throw "Existing audiobook tokens conflict. No files were changed."
}
$token = if ($appToken) {
    $appToken
} elseif ($haToken) {
    $haToken
} else {
    New-DashboardToken
}
if ($token -notmatch "^[A-Za-z0-9_-]{32,128}$") {
    throw "The existing audiobook token is not safe for dotenv and Nginx use."
}

# The panel iframe needs a URL a browser will load inside Home Assistant, which
# is served over https -- so an http URL is refused outright as mixed content.
# The derived public URL is https, but it sits behind the proxy's Basic auth,
# and a browser cannot satisfy that inside an iframe. A Tailscale `serve`
# endpoint solves both: real certificate, no Basic auth, tailnet only.
#
# So do not overwrite one that is already configured. Re-running this script
# after setting up `tailscale serve` used to silently put the unusable public
# URL back.
$existingExternalUrl = Get-YamlScalar $haSecretsPath "audiobook_external_url"
if ($existingExternalUrl -and $existingExternalUrl -match "^https://[^/]+\.ts\.net(/|$)") {
    $audiobookExternalUrl = $existingExternalUrl
    Write-Output "Kept the existing Tailscale panel URL instead of replacing it with the public one."
}
else {
    $audiobookExternalUrl = $haExternalUrl.TrimEnd("/") + "/audiobook/"
}
$healthUrl = "http://${crazyPcAddress}:8000/health"
$releaseGpuUrl = "http://${crazyPcAddress}:8000/api/system/release-gpu"

Set-DotEnvValue `
    $appEnvironmentPath `
    "CRAZY_AUDIOBOOK_DASHBOARD_TOKEN" `
    $token
Set-YamlScalar $haSecretsPath "audiobook_external_url" $audiobookExternalUrl
Set-YamlScalar $haSecretsPath "audiobook_health_url" $healthUrl
Set-YamlScalar $haSecretsPath "audiobook_release_gpu_url" $releaseGpuUrl
Set-YamlScalar $haSecretsPath "audiobook_api_token" $token

$npmTemplate = @'
# Generated by scripts/configure_home_assistant_integration.ps1.
# Contains a secret upstream token. This file is intentionally gitignored.
#
# This fail-closed version expects /data/audiobook.htpasswd to exist in NPM.
location = /audiobook {
    return 301 /audiobook/;
}

location ^~ /audiobook/ {
    auth_basic "Crazy Audiobook Creator";
    auth_basic_user_file /data/audiobook.htpasswd;

    proxy_pass http://__CRAZY_PC_ADDRESS__:8000/;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header X-API-Token "__DASHBOARD_TOKEN__";

    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
    proxy_cache off;
    client_max_body_size 100m;
}
'@
$npmConfig = $npmTemplate.
    Replace("__CRAZY_PC_ADDRESS__", $crazyPcAddress).
    Replace("__DASHBOARD_TOKEN__", $token)
[System.IO.File]::WriteAllText(
    $npmConfigPath,
    ($npmConfig.TrimEnd() + [Environment]::NewLine),
    $utf8NoBom
)

Write-Output "Configured the existing audiobook .env without displaying its token."
Write-Output "Configured Home Assistant secrets without displaying addresses or credentials."
Write-Output "Generated the ignored NPM location file at '$npmConfigPath'."
Write-Output "The detected HA, proxy, and Crazy-PC addresses were retained only in ignored local files."

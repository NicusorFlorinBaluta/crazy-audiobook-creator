# Crazy Audiobook Creator - Desktop Shortcut Generator

$ProjectRoot = $PSScriptRoot
$DesktopPath = [System.Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $DesktopPath "Crazy Audiobook Creator.lnk"
$TargetScript = Join-Path $ProjectRoot "start_app.pyw"
$PythonW = "E:\PYTORC~1\my_venv\Scripts\pythonw.exe"

if (-not (Test-Path $PythonW)) {
    $PythonW = "pythonw.exe"
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PythonW
$Shortcut.Arguments = "`"$TargetScript`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Crazy Audiobook Creator - AI-Powered Pipeline"

$FaviconPath = Join-Path $ProjectRoot "brain\dashboard\frontend\img\favicon.png"
if (Test-Path $FaviconPath) {
    $Shortcut.IconLocation = "$FaviconPath,0"
}

$Shortcut.Save()
Write-Host "Created Desktop Shortcut at: $ShortcutPath"

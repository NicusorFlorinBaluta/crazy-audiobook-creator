# Creates a Desktop Shortcut for Crazy Audiobook Creator
$ProjectRoot = $PSScriptRoot
$ShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Crazy Audiobook Creator.lnk"

$PythonW = Join-Path $ProjectRoot "venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $PythonW = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}

$TargetPath = $PythonW
$Arguments = "`"$ProjectRoot\start_app.pyw`""

$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.Arguments = $Arguments
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Crazy Audiobook Creator Desktop Application"

$IconPath = Join-Path $ProjectRoot "brain\dashboard\frontend\img\favicon.png"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}

$Shortcut.Save()
Write-Host "Desktop shortcut successfully created at: $ShortcutPath"

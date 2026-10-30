# 创建桌面「VocoType 语音输入」「停止 VocoType」快捷方式，以及开机自动启动
#   powershell -ExecutionPolicy Bypass -File install-shortcuts.ps1
#   powershell -ExecutionPolicy Bypass -File install-shortcuts.ps1 -NoAutostart   不要开机自动启动
param([switch]$NoAutostart)

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ps1 = Join-Path $dir 'start-vocotype.ps1'
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$startup = [Environment]::GetFolderPath('Startup')
$icon = Join-Path $dir '.venv\Scripts\python.exe'

function New-Link($path, $extra, $desc) {
    $l = $ws.CreateShortcut($path)
    $l.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $l.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`" $extra"
    $l.WorkingDirectory = $dir
    $l.WindowStyle = 7
    $l.Description = $desc
    if (Test-Path $icon) { $l.IconLocation = "$icon,0" }
    $l.Save()
    $path
}
New-Link (Join-Path $desktop 'VocoType 语音输入.lnk') '' '启动 VocoType 语音输入（按住 F2 说话）'
New-Link (Join-Path $desktop '停止 VocoType.lnk') '-Stop' '停止 VocoType 语音输入'
if (-not $NoAutostart) {
    New-Link (Join-Path $startup 'VocoType 语音输入.lnk') '-Quiet' '开机自动启动 VocoType 语音输入'
}

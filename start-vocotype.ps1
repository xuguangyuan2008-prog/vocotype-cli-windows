# VocoType 启动 / 停止（隐藏窗口在后台跑，日志在 logs\）
#   start-vocotype.ps1          手动启动：已经在跑就提示，不开第二个（两个会把同一句打两遍）
#   start-vocotype.ps1 -Quiet   开机自动启动：不弹提示
#   start-vocotype.ps1 -Stop    停止
param([switch]$Quiet, [switch]$Stop)

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$shell = New-Object -ComObject WScript.Shell

# 虚拟环境的 python.exe 只是个壳，会再起一个真正的 python（子进程）跑 main.py：认壳，停的时候连子进程一起停
function Get-Voco {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*main.py*' -and $_.ExecutablePath -like "$dir\*" }
}

if ($Stop) {
    $p = @(Get-Voco)
    if ($p.Count) {
        $ids = $p | ForEach-Object { $_.ProcessId }
        $kids = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $ids -contains $_.ParentProcessId }
        @($kids) + $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        $shell.Popup('VocoType 已停止', 3, 'VocoType', 64) | Out-Null
    } else {
        $shell.Popup('VocoType 没在运行', 3, 'VocoType', 64) | Out-Null
    }
    exit
}

if (@(Get-Voco).Count) {
    if (-not $Quiet) { $shell.Popup('VocoType 已经在运行了，按住 F2 说话，松开结束', 4, 'VocoType', 64) | Out-Null }
    exit
}

$env:PYTHONIOENCODING = 'utf-8'
Start-Process -FilePath "$dir\.venv\Scripts\python.exe" -ArgumentList 'main.py' -WorkingDirectory $dir -WindowStyle Hidden
if (-not $Quiet) { $shell.Popup('VocoType 正在启动，大约 30 秒后按住 F2 说话，松开结束', 4, 'VocoType', 64) | Out-Null }

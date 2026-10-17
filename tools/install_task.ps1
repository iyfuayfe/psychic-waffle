# 注册计划任务：登录后自动在后台静默运行校园网自动登录
# 用 pythonw.exe 启动 => 没有控制台窗口；程序自身不弹任何对话框。
#
# 用法（普通权限即可，不需要管理员）：
#   powershell -ExecutionPolicy Bypass -File tools\install_task.ps1
#
[CmdletBinding()]
param(
    [string]$TaskName = 'CampusNetAutoLogin',
    [string]$PythonwPath = '',
    [int]$RepeatMinutes = 15
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot 'campus_login.py'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "找不到 campus_login.py：$scriptPath"
}

# ---------- 定位 pythonw.exe ----------
if (-not $PythonwPath) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $PythonwPath = $cmd.Source }
}
if (-not $PythonwPath) {
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = Join-Path (Split-Path -Parent $py.Source) 'pythonw.exe'
        if (Test-Path -LiteralPath $candidate) { $PythonwPath = $candidate }
    }
}
if (-not $PythonwPath -or -not (Test-Path -LiteralPath $PythonwPath)) {
    throw '找不到 pythonw.exe。请确认已安装 Python 且 pythonw.exe 在 PATH 中。'
}

$me = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "程序 : $PythonwPath"
Write-Host "脚本 : $scriptPath"
Write-Host "用户 : $me"

# ---------- 方案 A：ScheduledTasks 模块（功能最全，推荐）----------
function Install-ViaModule {
    $action = New-ScheduledTaskAction -Execute $PythonwPath `
        -Argument ('"' + $scriptPath + '"') `
        -WorkingDirectory $projectRoot

    $triggers = @()
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User $me
    $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description '校园网自动登录（后台静默运行，无窗口无弹窗）' `
        -Force | Out-Null
}

# ---------- 方案 B：schtasks.exe 兜底（模块不可用时）----------
# 需要两个任务：一个登录时触发，一个每 N 分钟自愈一次。
function Install-ViaSchtasks {
    $tr = '\"' + $PythonwPath + '\" \"' + $scriptPath + '\"'

    & schtasks.exe /Create /TN $TaskName /TR $tr /SC ONLOGON /RL LIMITED /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks 注册登录触发失败（退出码 $LASTEXITCODE）" }

    & schtasks.exe /Create /TN "$TaskName-KeepAlive" /TR $tr /SC MINUTE /MO $RepeatMinutes /RL LIMITED /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks 注册自愈触发失败（退出码 $LASTEXITCODE）" }
}

# ---------- 执行 ----------
$mode = ''
try {
    Install-ViaModule
    $mode = 'ScheduledTasks 模块'
} catch {
    Write-Warning "计划任务模块注册失败：$($_.Exception.Message)"
    Write-Host '改用 schtasks.exe 兜底方案 ...'
    Install-ViaSchtasks
    $mode = 'schtasks.exe'
}

Write-Host ''
Write-Host "已注册计划任务：$TaskName（方式：$mode）" -ForegroundColor Green
Write-Host "  触发：用户登录时 + 每 $RepeatMinutes 分钟自愈检查"
Write-Host '  （进程已在跑时，新实例会因单实例互斥自行退出，不会重复登录）'
Write-Host ''
Write-Host "查看状态 : schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "立即启动 : schtasks /Run /TN $TaskName"
Write-Host "停止     : schtasks /End /TN $TaskName"
Write-Host "查看日志 : Get-Content '$projectRoot\logs\campus_login.log' -Tail 30 -Encoding UTF8"
Write-Host '卸载     : powershell -ExecutionPolicy Bypass -File tools\uninstall_task.ps1'

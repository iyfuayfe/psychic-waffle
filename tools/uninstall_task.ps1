# 卸载：删除计划任务并结束正在运行的后台登录进程
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\uninstall_task.ps1
#
[CmdletBinding()]
param(
    [string]$TaskName = 'CampusNetAutoLogin',
    [switch]$KeepProcess
)

$ErrorActionPreference = 'Continue'

# ---------- 1. 删除计划任务 ----------
$names = @($TaskName, "$TaskName-KeepAlive")
foreach ($name in $names) {
    $removed = $false

    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task) {
            try { Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue } catch {}
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
            $removed = $true
        }
    } catch {
        Write-Host "计划任务模块删除失败：$($_.Exception.Message)"
    }

    if (-not $removed) {
        & schtasks.exe /Query /TN $name 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            & schtasks.exe /Delete /TN $name /F | Out-Null
            if ($LASTEXITCODE -eq 0) { $removed = $true }
        }
    }

    if ($removed) {
        Write-Host "已删除计划任务：$name" -ForegroundColor Green
    } else {
        Write-Host "未发现计划任务：$name"
    }
}

# ---------- 1.5 删除启动文件夹里的双保险脚本 ----------
$startupVbs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\campus-net-autologin.vbs'
if (Test-Path -LiteralPath $startupVbs) {
    try {
        Remove-Item -LiteralPath $startupVbs -Force -ErrorAction Stop
        Write-Host '已删除启动文件夹脚本：campus-net-autologin.vbs' -ForegroundColor Green
    } catch {
        Write-Host "启动文件夹脚本删除失败：$($_.Exception.Message)"
    }
} else {
    Write-Host '未发现启动文件夹脚本（双保险）。'
}

# ---------- 1.8 移除本程序加过的杀毒白名单 ----------
# 需要管理员权限：如弹出 UAC 窗口请点「是」；点「否」则跳过（不影响卸载本身）。
$wlScript = Join-Path $PSScriptRoot 'whitelist.ps1'
if (Test-Path -LiteralPath $wlScript) {
    Write-Host '移除本程序加过的杀毒白名单 ...'
    $wlResult = Join-Path $env:TEMP ("campusnet-wl-uninstall-$PID.json")
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $wlScript -Remove -ResultFile $wlResult
    if (Test-Path -LiteralPath $wlResult) {
        Remove-Item -LiteralPath $wlResult -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host '未找到白名单脚本，跳过白名单清理。'
}

# ---------- 2. 结束本程序的后台进程（只杀命令行里带 campus_login.py 的）----------
if (-not $KeepProcess) {
    $killed = 0
    try {
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like '*campus_login.py*' } |
            ForEach-Object {
                try {
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                    Write-Host "已结束进程 PID $($_.ProcessId)" -ForegroundColor Green
                    $killed++
                } catch {}
            }
    } catch {}
    if ($killed -eq 0) { Write-Host '没有发现正在运行的后台登录进程。' }
}

Write-Host ''
Write-Host '配置文件 config.json 和日志 logs\ 已保留，如需彻底清理请手动删除。'

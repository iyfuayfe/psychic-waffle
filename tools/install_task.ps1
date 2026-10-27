# 已废弃（V3.0）：请改用 CampusNet 图形界面或命令行。
#
# V2 用「计划任务 + 启动文件夹 VBS」双保险自启动，问题有三点：
#   1. 注册登录触发型计划任务通常需要管理员权限，普通用户装不上；
#   2. 两套机制互相不知道对方，只能靠互斥体兜底，出问题难解释；
#   3. 那份 VBS 还把 python.exe 的绝对路径写死了，换台机器直接失效。
#
# V3.0 简化为**唯一**一种用户级机制：HKCU\...\Run 里一个值
# （CampusNetDaemon = "...CampusNetService.exe" --daemon），
# 不需要管理员权限，删起来也干净。
#
# 这个脚本保留下来只为兼容旧文档/旧习惯，实际作用是转调新的自启动实现。
#
# 用法（普通权限即可）：
#   powershell -ExecutionPolicy Bypass -File tools\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install_task.ps1 -Disable
#
[CmdletBinding()]
param(
    [switch]$Disable
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

Write-Host ''
Write-Host '[提示] 这个脚本已改为转调 V3.0 的自启动实现（HKCU Run，用户级，无需管理员）。' -ForegroundColor Yellow
Write-Host '       旧版计划任务会被自动清理；更推荐直接用界面「设置 → 开机自动启动后台守护」。'
Write-Host ''

$cli = $null
foreach ($candidate in @(
        (Join-Path $root 'CampusNetCLI.exe'),
        (Join-Path $root 'dist\CampusNet\CampusNetCLI.exe'))) {
    if (Test-Path -LiteralPath $candidate) { $cli = $candidate; break }
}

if ($cli) {
    if ($Disable) {
        & $cli autostart off
    } else {
        & $cli autostart on
    }
    exit $LASTEXITCODE
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host '[错误] 既没有 CampusNetCLI.exe，也没有安装 Python。' -ForegroundColor Red
    Write-Host '       请使用发布包里的 CampusNet.exe，或先安装 Python 3.9+。'
    exit 1
}

if ($Disable) {
    & $python.Source (Join-Path $root 'entry.py') --role cli autostart off
} else {
    & $python.Source (Join-Path $root 'entry.py') --role cli autostart on
}
exit $LASTEXITCODE

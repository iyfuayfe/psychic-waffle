# 已废弃（V3.0）：卸载请用「3-卸载.bat」或 CampusNetCLI.exe uninstall
#
# 这个脚本保留下来只为兼容旧文档/旧习惯，实际作用是转调 V3.0 的卸载实现：
#   1. 优雅停止后台守护（写 stop.flag，等它自己退出，不 taskkill 打断请求）；
#   2. 删除 HKCU Run 里的自启动值；
#   3. 清理 V2 留下的计划任务与启动文件夹脚本（只删精确名字）；
#   4. 默认**保留**用户配置与日志。
#
# 不修改任何杀毒软件设置，也不动其它程序的文件与配置。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\uninstall_task.ps1
#   powershell -ExecutionPolicy Bypass -File tools\uninstall_task.ps1 -RemoveData
#
[CmdletBinding()]
param(
    [switch]$RemoveData
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

Write-Host ''
Write-Host '[提示] 这个脚本已改为转调 V3.0 的卸载实现（不需要管理员权限）。' -ForegroundColor Yellow
Write-Host ''

$cli = $null
foreach ($candidate in @(
        (Join-Path $root 'CampusNetCLI.exe'),
        (Join-Path $root 'dist\CampusNet\CampusNetCLI.exe'))) {
    if (Test-Path -LiteralPath $candidate) { $cli = $candidate; break }
}

$arguments = @('uninstall')
if ($RemoveData) { $arguments += '--remove-data' }

if ($cli) {
    & $cli @arguments
    exit $LASTEXITCODE
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host '[错误] 既没有 CampusNetCLI.exe，也没有安装 Python。' -ForegroundColor Red
    exit 1
}

& $python.Source (Join-Path $root 'entry.py') --role cli @arguments
exit $LASTEXITCODE

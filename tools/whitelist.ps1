# 校园网自动登录 —— 加入杀毒软件白名单
#
# 作用：
#   - Windows Defender：自动把 pythonw.exe / python.exe / pyw.exe / py.exe（进程）
#     以及程序文件夹、Python 安装目录、启动文件夹脚本（路径）加入排除项。
#     需要管理员权限，脚本会自动请求 UAC 提权。
#   - 360 / 火绒 / 腾讯电脑管家等第三方杀毒：它们没有公开的白名单接口，
#     脚本会检测出来并打印"手动添加"的步骤，照着点几下即可。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\whitelist.ps1 -Check      # 只看现状（无需管理员）
#   powershell -ExecutionPolicy Bypass -File tools\whitelist.ps1 -Apply      # 加入白名单（会弹 UAC）
#   powershell -ExecutionPolicy Bypass -File tools\whitelist.ps1 -Remove     # 移除本程序加过的白名单（卸载用）
#   加 -ResultFile <路径>：把结果（UTF-8 JSON）写到指定文件，GUI 从文件读结果。
#
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Remove,
    [switch]$Check,
    [switch]$NoElevate,
    [string]$ResultFile = ''
)

$ErrorActionPreference = 'Continue'

# ---------- 要加白名单的对象 ----------
$projectRoot = Split-Path -Parent $PSScriptRoot

$pythonw = ''
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) { $pythonw = $cmd.Source }
if (-not $pythonw) {
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) {
        $cand = Join-Path (Split-Path -Parent $py.Source) 'pythonw.exe'
        if (Test-Path -LiteralPath $cand) { $pythonw = $cand }
    }
}
$pyDir = ''
if ($pythonw) {
    $pyDir = Split-Path -Parent $pythonw
    # 系统目录里的 Python（如应用商店版）整目录排除不合适，跳过
    if ($pyDir.ToLowerInvariant().StartsWith(($env:windir + '\').ToLowerInvariant())) { $pyDir = '' }
}

$startupVbs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\campus-net-autologin.vbs'

$wantProcesses = @('pythonw.exe', 'python.exe', 'pyw.exe', 'py.exe')
$wantPaths = @($projectRoot)
if ($pyDir) { $wantPaths += $pyDir }
if (Test-Path -LiteralPath $startupVbs) { $wantPaths += $startupVbs }

# ---------- 基础能力 ----------

$script:_avCache = $null
$script:_defCache = $null

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Get-AvList {
    if ($null -ne $script:_avCache) { return $script:_avCache }
    $names = @()
    try {
        Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction Stop |
            ForEach-Object {
                $n = "$($_.displayName)".Trim()
                # 过滤掉 SecurityCenter2 里冒充杀毒软件的插件（如微信/QQ 邮箱插件）
                if ($n -and $n -notmatch 'QQMail|Plugin|插件') { $names += $n }
            }
    } catch { }
    if (-not $names) {
        # 兜底：从卸载注册表里找已知杀毒软件
        $keywords = @('360安全', '360杀毒', '火绒', 'HuoRong', '电脑管家', 'QQPCMgr', 'PC Manager', 'Kaspersky', 'ESET',
                      'Avast', 'AVG', 'McAfee', 'Norton', 'Bitdefender', 'Malwarebytes',
                      '趋势', 'Trend Micro', '瑞星', 'Rising', 'Avira')
        foreach ($root in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
                            'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
            try {
                $found = Get-ItemProperty $root -ErrorAction SilentlyContinue |
                    Where-Object { $_.DisplayName } |
                    Where-Object {
                        $dn = $_.DisplayName
                        @($keywords | Where-Object { $dn -like "*$_*" }).Count -gt 0
                    } |
                    ForEach-Object { "$($_.DisplayName)".Trim() }
                foreach ($n in $found) {
                    if ($n -and ($names -notcontains $n)) { $names += $n }
                }
            } catch { }
        }
    }
    $script:_avCache = @($names | Select-Object -Unique)
    return $script:_avCache
}

function Get-DefenderInfo {
    if ($null -ne $script:_defCache) { return $script:_defCache }
    $info = @{ Available = $false; Active = $false; Error = '' }
    try {
        $st = Get-MpComputerStatus -ErrorAction Stop
        $info.Available = $true
        $info.Active = [bool]$st.AMServiceEnabled
    } catch {
        $info.Error = "$($_.Exception.Message)"
    }
    $script:_defCache = $info
    return $info
}

function Get-Exclusions {
    $r = @{ Processes = @(); Paths = @(); Error = '' }
    try {
        $pref = Get-MpPreference -ErrorAction Stop
        $r.Processes = @($pref.ExclusionProcess | Where-Object { $_ }) | ForEach-Object { "$_".Trim() }
        $r.Paths = @($pref.ExclusionPath | Where-Object { $_ }) | ForEach-Object { "$_".Trim() }
    } catch {
        $r.Error = "$($_.Exception.Message)"
    }
    return $r
}

function Get-Missing {
    param($HaveProcesses, $HavePaths)
    $mp = @($wantProcesses | Where-Object {
        $p = $_
        -not ($HaveProcesses | Where-Object { $_.ToLowerInvariant() -eq $p.ToLowerInvariant() })
    })
    $mPaths = @($wantPaths | Where-Object {
        $p = $_
        -not ($HavePaths | Where-Object { $_.TrimEnd('\').ToLowerInvariant() -eq $p.TrimEnd('\').ToLowerInvariant() })
    })
    return @{ Processes = @($mp); Paths = @($mPaths) }
}

function Split-Av {
    param($Names)
    $def = @(); $third = @()
    foreach ($n in $Names) {
        if ($n -match 'Defender|Windows') { $def += $n } else { $third += $n }
    }
    return @{ Defender = @($def); ThirdParty = @($third) }
}

function Get-ThirdPartyHint {
    param($ThirdPartyNames)
    if (-not $ThirdPartyNames -or $ThirdPartyNames.Count -eq 0) { return '' }
    $lines = @('检测到第三方杀毒软件，它们没有公开的白名单接口，请按下面步骤手动添加：')
    foreach ($av in $ThirdPartyNames) {
        switch -Wildcard ($av) {
            '*360*'    { $lines += "· $av：打开主界面 → 木马查杀（或 防护中心）→ 信任区 → 添加目录，把下面的文件夹加进去，并把 pythonw.exe 加入信任"; break }
            '*火绒*'    { $lines += "· $av：打开主界面 → 病毒防护 → 信任区 → 添加文件夹，把下面的文件夹加进去"; break }
            '*HuoRong*' { $lines += "· $av：Huorong → 病毒防护 → 信任区 → 添加文件夹，把下面的文件夹加进去"; break }
            '*腾讯*'    { $lines += "· $av：打开主界面 → 病毒查杀 → 信任区 → 添加文件夹，把下面的文件夹加进去"; break }
            default     { $lines += "· $av：在它的『信任区 / 白名单 / 排除项』里添加下面的文件夹和进程" }
        }
    }
    $lines += '  文件夹 1（程序目录）：' + $projectRoot
    if ($pyDir) { $lines += '  文件夹 2（Python 安装目录）：' + $pyDir }
    $lines += '  进程：pythonw.exe（另有 python.exe / pyw.exe / py.exe）'
    return ($lines -join "`n")
}

function Add-WhitelistEntries {
    $appliedP = @(); $appliedPaths = @(); $errors = @()
    foreach ($p in $wantProcesses) {
        try {
            Add-MpPreference -ExclusionProcess $p -ErrorAction Stop
            $appliedP += $p
        } catch {
            $errors += "排除进程 $p 失败：$($_.Exception.Message)"
        }
    }
    foreach ($p in $wantPaths) {
        try {
            Add-MpPreference -ExclusionPath $p -ErrorAction Stop
            $appliedPaths += $p
        } catch {
            $errors += "排除路径 $p 失败：$($_.Exception.Message)"
        }
    }
    return @{ Processes = @($appliedP); Paths = @($appliedPaths); Errors = @($errors) }
}

function Remove-WhitelistEntries {
    foreach ($p in $wantProcesses) {
        try { Remove-MpPreference -ExclusionProcess $p -ErrorAction SilentlyContinue } catch { }
    }
    foreach ($p in $wantPaths) {
        try { Remove-MpPreference -ExclusionPath $p -ErrorAction SilentlyContinue } catch { }
    }
}

function Build-Result {
    param(
        [bool]$Ok,
        [string]$Summary = '',
        [bool]$Cancelled = $false,
        $AppliedProcesses = @(),
        $AppliedPaths = @(),
        $Errors = @()
    )
    $av = @(Get-AvList)
    $split = Split-Av -Names $av
    $defInfo = Get-DefenderInfo
    $excl = Get-Exclusions
    $missing = Get-Missing -HaveProcesses $excl.Processes -HavePaths $excl.Paths
    $obj = [ordered]@{
        ok = $Ok
        cancelled = $Cancelled
        admin = (Test-Admin)
        summary = $Summary
        av = @($av)
        third_party_av = @($split.ThirdParty)
        defender_available = $defInfo.Available
        defender_active = $defInfo.Active
        defender_error = $defInfo.Error
        excluded_processes = @($excl.Processes)
        excluded_paths = @($excl.Paths)
        missing_processes = @($missing.Processes)
        missing_paths = @($missing.Paths)
        applied_processes = @($AppliedProcesses)
        applied_paths = @($AppliedPaths)
        errors = @($Errors)
        third_party_hint = (Get-ThirdPartyHint -ThirdPartyNames $split.ThirdParty)
    }
    return $obj
}

function Write-ResultFile {
    param($Obj)
    if (-not $ResultFile) { return }
    try {
        $json = $Obj | ConvertTo-Json -Depth 5 -Compress
        Set-Content -LiteralPath $ResultFile -Value $json -Encoding UTF8
    } catch { }
}

function Build-ApplySummary {
    param($AppliedProcesses, $AppliedPaths, $Errors, $ThirdPartyNames)
    $lines = @()
    if (($AppliedProcesses.Count + $AppliedPaths.Count) -gt 0) {
        $lines += '完成：Windows Defender 白名单已添加。'
        if ($AppliedProcesses.Count -gt 0) { $lines += '  排除进程：' + ($AppliedProcesses -join '、') }
        if ($AppliedPaths.Count -gt 0) { $lines += '  排除路径：' + ($AppliedPaths -join '；') }
    } else {
        $lines += '没有需要新增的项目（可能已经全部在白名单里）。'
    }
    if ($Errors.Count -gt 0) {
        $lines += '以下项目添加失败：'
        foreach ($e in $Errors) { $lines += '  · ' + $e }
    }
    if ($ThirdPartyNames -and $ThirdPartyNames.Count -gt 0) {
        $lines += ''
        $lines += Get-ThirdPartyHint -ThirdPartyNames $ThirdPartyNames
    }
    return ($lines -join "`n")
}

# ---------- 需要管理员时自我提权 ----------
if (($Apply -or $Remove) -and -not (Test-Admin) -and -not $NoElevate) {
    $hostExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $hostExe)) {
        $c = Get-Command powershell.exe -ErrorAction SilentlyContinue
        if ($c) { $hostExe = $c.Source }
        else {
            try { $hostExe = (Get-Process -Id $PID).Path } catch { $hostExe = '' }
        }
    }
    if (-not $hostExe) { $hostExe = 'powershell.exe' }

    $argList = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
    if ($Apply)  { $argList += ' -Apply' }
    if ($Remove) { $argList += ' -Remove' }
    $argList += ' -NoElevate'
    if ($ResultFile) { $argList += ' -ResultFile "' + $ResultFile + '"' }

    try {
        if ($ResultFile) {
            # GUI 场景：提权进程不显示窗口，结果由子进程写进 ResultFile
            $proc = Start-Process -FilePath $hostExe -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList $argList
        } else {
            $proc = Start-Process -FilePath $hostExe -Verb RunAs -Wait -PassThru -ArgumentList $argList
        }
        if ($ResultFile -and (Test-Path -LiteralPath $ResultFile)) {
            try {
                $data = Get-Content -LiteralPath $ResultFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $sum = "$($data.summary)".Trim()
                if ($sum) { Write-Host $sum }
            } catch { }
        }
        exit $proc.ExitCode
    } catch {
        Write-Host '已取消：加入白名单需要管理员权限（在 UAC 窗口里请点『是』）。'
        if ($ResultFile) {
            $obj = Build-Result -Ok $false -Cancelled $true -Summary '已取消：需要管理员权限才能修改白名单（UAC 窗口请点『是』）。'
            Write-ResultFile -Obj $obj
        }
        exit 2
    }
}

# ---------- 执行：加入白名单 ----------
if ($Apply) {
    if (-not (Test-Admin)) {
        Write-Host '没有管理员权限，无法修改白名单。'
        $obj = Build-Result -Ok $false -Summary '没有管理员权限，无法修改白名单。'
        Write-ResultFile -Obj $obj
        exit 3
    }
    $defInfo = Get-DefenderInfo
    $split = Split-Av -Names @(Get-AvList)
    if (-not $defInfo.Available) {
        if ($split.ThirdParty -and $split.ThirdParty.Count -gt 0) {
            $summary = "Windows Defender 排除项接口不可用：$($defInfo.Error)`n`n" + (Get-ThirdPartyHint -ThirdPartyNames $split.ThirdParty)
        } else {
            $summary = "无法修改 Windows Defender 白名单：$($defInfo.Error)"
        }
        Write-Host $summary
        $obj = Build-Result -Ok $false -Summary $summary
        Write-ResultFile -Obj $obj
        exit 4
    }
    $r = Add-WhitelistEntries
    $summary = Build-ApplySummary -AppliedProcesses $r.Processes -AppliedPaths $r.Paths -Errors $r.Errors -ThirdPartyNames $split.ThirdParty
    $obj = Build-Result -Ok ($r.Errors.Count -eq 0) -Summary $summary -AppliedProcesses $r.Processes -AppliedPaths $r.Paths -Errors $r.Errors
    Write-ResultFile -Obj $obj
    Write-Host $summary
    if ($r.Errors.Count -gt 0) { exit 5 }
    exit 0
}

# ---------- 执行：只看现状 ----------
if ($Check) {
    $defInfo = Get-DefenderInfo
    $excl = Get-Exclusions
    $missing = Get-Missing -HaveProcesses $excl.Processes -HavePaths $excl.Paths
    $split = Split-Av -Names @(Get-AvList)
    $allOk = $defInfo.Available -and ($missing.Processes.Count -eq 0) -and ($missing.Paths.Count -eq 0)
    $obj = Build-Result -Ok $allOk -Summary ''
    Write-ResultFile -Obj $obj

    $avNames = @(Get-AvList)
    if ($avNames.Count -gt 0) { Write-Host ('检测到的杀毒软件：' + ($avNames -join '、')) }
    else { Write-Host '检测到的杀毒软件：（未读取到）' }
    if ($defInfo.Available) {
        $m = @($missing.Processes) + @($missing.Paths)
        if ($m.Count -eq 0) {
            Write-Host 'Windows Defender 白名单：已全部加入。'
        } else {
            Write-Host ('Windows Defender 白名单还缺：' + (($missing.Processes + $missing.Paths) -join '、'))
        }
    } else {
        Write-Host ('Windows Defender 排除项不可用：' + $defInfo.Error)
    }
    $hint = Get-ThirdPartyHint -ThirdPartyNames $split.ThirdParty
    if ($hint) { Write-Host $hint }
    exit 0
}

# ---------- 执行：移除白名单（卸载用） ----------
if ($Remove) {
    if (-not (Test-Admin)) {
        Write-Host '没有管理员权限，无法移除白名单。'
        exit 3
    }
    $defInfo = Get-DefenderInfo
    if (-not $defInfo.Available) {
        Write-Host "无法调用 Windows Defender 排除项接口：$($defInfo.Error)（白名单条目保持原样）"
        if ($ResultFile) {
            $obj = Build-Result -Ok $false -Summary '无法调用 Windows Defender 排除项接口，白名单条目保持原样。'
            Write-ResultFile -Obj $obj
        }
        exit 4
    }
    Remove-WhitelistEntries
    $msg = '已移除本程序添加的 Windows Defender 白名单条目（pythonw.exe / python.exe / pyw.exe / py.exe 及程序文件夹、Python 目录、启动脚本）。'
    Write-Host $msg
    if ($ResultFile) {
        $obj = Build-Result -Ok $true -Summary $msg
        Write-ResultFile -Obj $obj
    }
    exit 0
}

Write-Host '用法：whitelist.ps1 -Check | -Apply | -Remove'

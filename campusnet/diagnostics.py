"""只读诊断：安全现状体检 + 脱敏诊断包导出。

V3.0 明确的立场：**不自动往杀毒软件里加任何排除项**。

* 普通启动、网络检测、读配置、看日志都不需要管理员权限，也不该触发 UAC；
* 遇到"被安全软件拦了"的怀疑时，正确做法是**先拿到证据**（程序是否在跑、
  启动项是什么、日志里有什么异常），再让用户自己去安全软件里查看报告；
* 本模块只做三件事：读现状、给出可操作的建议、导出脱敏诊断包。

全部只读、无需管理员，失败一律返回可读的文字而不是抛异常。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import autostart, paths, wifi
from .config import load_heartbeat, load_state
from .security import redact_text
from .version import banner

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(args: list, timeout: float = 25.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    for encoding in ("utf-8", "mbcs", "gbk"):
        try:
            return proc.returncode, raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return proc.returncode, raw.decode("utf-8", "replace")


def antivirus_products() -> list:
    """读 Windows 安全中心登记的杀毒软件（只读，无管理员）。"""
    script = (
        "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct "
        "-ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty displayName"
    )
    code, text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if code != 0 or not text.strip():
        return []
    names = []
    for line in text.splitlines():
        name = line.strip()
        if name and name not in names:
            names.append(name)
    return names


def defender_status() -> dict:
    """Defender 是否在正常工作（只读；不可用时给出原因，不尝试修改任何设置）。"""
    script = (
        "try { $s = Get-MpComputerStatus -ErrorAction Stop; "
        "[pscustomobject]@{ok=$true; realtime=[bool]$s.RealTimeProtectionEnabled; "
        "antivirus=[bool]$s.AntivirusEnabled} | ConvertTo-Json -Compress } "
        "catch { [pscustomobject]@{ok=$false; error=\\\"$($_.Exception.Message)\\\"} "
        "| ConvertTo-Json -Compress }"
    )
    code, text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if code != 0 or not text.strip():
        return {"ok": False, "error": text.strip() or "无法读取 Defender 状态"}
    try:
        data = json.loads(text.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "无法解析 Defender 状态输出"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "Defender 状态输出格式异常"}
    return data


def our_exclusions_present() -> list:
    """检查是否有人把**本程序自身**加进了 Defender 排除项。

    只做只读检查，用于向用户说明"如果之前加过，现在可以自己移除"。
    """
    script = (
        "$p = Get-MpPreference -ErrorAction SilentlyContinue; "
        "if ($p) { ($p.ExclusionProcess + $p.ExclusionPath) -ne $null | "
        "ForEach-Object { $_ } }"
    )
    code, text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    if code != 0 or not text.strip():
        return []
    needles = ("pythonw", "python.exe", "pyw.exe", "campusnet", "campus-net-login", "campus_login")
    hits = []
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        low = item.lower()
        if any(needle in low for needle in needles):
            hits.append(item)
    return hits


def security_report() -> dict:
    """界面"安全体检"按钮背后的函数：全部只读。"""
    av = antivirus_products()
    defender = defender_status()
    legacy = autostart.legacy_present()
    report = {
        "products": av,
        "defender": defender,
        "autostart": autostart.status_dict(),
        "legacy": legacy,
        "exclusions_hint": our_exclusions_present(),
        "admin_required": False,
        "advice": [],
    }

    advice = report["advice"]
    if defender.get("ok") and defender.get("realtime"):
        advice.append("Windows Defender 实时保护正常，本程序不需要任何排除项即可运行。")
    if report["exclusions_hint"]:
        advice.append(
            "检测到本程序相关路径或解释器在 Defender 排除项里。V3.0 不再自动添加排除项，"
            "如果这是旧版加的，可以自己移除（Windows 安全中心 → 病毒和威胁防护 → "
            "管理设置 → 排除项）。移除后程序照常工作。"
        )
    if legacy["tasks"] or legacy["startup_scripts"]:
        advice.append(
            "发现旧版（V2）留下的自启动项："
            + "、".join(legacy["tasks"] + legacy["startup_scripts"])
            + "。V3.0 只用一个自启动项，可以在「设置」页点「清理旧版自启动」删掉它们。"
        )
    advice.append(
        "如果怀疑被安全软件拦截：先看「后台守护」是否运行、再看日志里有没有异常退出记录，"
        "最后到安全软件自己的「防护记录 / 隔离区」里查有没有本程序的文件。"
        "不要为了排障直接关闭安全软件。"
    )
    return report


def collect_snapshot() -> str:
    """收集一份**已脱敏**的诊断文本。"""
    from .config import Config

    try:
        cfg = Config.load(paths.config_path())
    except Exception:  # noqa: BLE001  配置坏了也要能导出诊断包
        cfg = Config()
    sections: list = []
    add = sections.append
    add(f"# {banner()} 诊断包")
    add(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    add("")
    add("## 路径")
    for key, value in paths.describe().items():
        add(f"{key} = {value}")
    add("")
    add("## 运行形态")
    add(f"frozen = {paths.is_frozen()}")
    add(f"portable = {paths.is_portable()}")
    add("")
    add("## 第三方注入模块 / 上次运行")
    try:
        from . import conflicts, session

        hits = conflicts.detect()
        if hits:
            add("检测到以下会注入本进程的模块（与界面闪退有关）：")
            for hit in hits:
                add(f"  {hit.module} | {hit.vendor} | 已确认导致闪退={hit.verified}")
                add(f"     路径: {hit.path}")
                add(f"     建议: {hit.advice}")
        else:
            add("没有检测到已知的叠加类注入模块。")
        previous = session.previous()
        add("上次运行：" + (session.describe_previous(previous) or "正常退出（无异常标记）"))
    except Exception as exc:  # noqa: BLE001
        add(f"（读取失败：{exc}）")
    add("")
    add("## 自启动与后台")
    for key, value in autostart.status_dict().items():
        add(f"{key} = {value}")
    add("")
    add("## 配置（已脱敏）")
    try:
        add(json.dumps(cfg.redacted(), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        add(f"（读取失败：{exc}）")
    add("")
    add("## 运行状态")
    add(json.dumps(load_heartbeat(), ensure_ascii=False, indent=2))
    add(json.dumps(load_state(), ensure_ascii=False, indent=2))
    add("")
    add("## 无线网络")
    try:
        status = wifi.interface_status()
        add(f"available={status.available} connected={status.connected} "
            f"ssid={status.ssid} signal={status.signal} error={status.error}")
    except Exception as exc:  # noqa: BLE001
        add(f"（读取失败：{exc}）")
    add("")
    add("## 安全软件")
    try:
        report = security_report()
        add(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        add(f"（读取失败：{exc}）")
    add("")
    add("## 最近日志（已脱敏）")
    try:
        log_path = paths.log_file()
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            add("\n".join(lines[-200:]))
        else:
            add("（还没有日志文件）")
    except Exception as exc:  # noqa: BLE001
        add(f"（读取失败：{exc}）")

    text = "\n".join(sections)
    # 最后再整体脱敏一次：日志与配置里可能混进别的东西
    return redact_text(text, keep_ip=False)


def export_bundle(directory: Path | None = None) -> Path:
    """把诊断包写到 ``diagnostics/diag-<时间戳>.txt`` 并返回路径。"""
    target_dir = directory or paths.diagnostics_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"diag-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    path.write_text(collect_snapshot(), encoding="utf-8", errors="replace")
    return path


def open_data_dir() -> bool:
    try:
        os.startfile(str(paths.data_dir()))  # noqa: S606
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default

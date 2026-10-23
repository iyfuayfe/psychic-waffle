"""界面与命令行共用的辅助函数：自启动、进程状态、日志、安全体检。

历史沿革：V2 里这个模块叫 ``tools/gui_common.py``，包含"一键加 Defender 白名单"
的实现（会自动把 python.exe / 整个项目目录加进排除项）。**V3.0 已删除该能力**，
现在这里只提供：

* 自启动开关（HKCU Run，唯一机制）
* 后台进程状态（互斥体 + 心跳文件）
* 日志读取、上次成功时间
* 只读的安全体检与脱敏诊断包导出

保留新旧两套函数名，是为了让旧的脚本/界面代码在过渡期不会因为
``ImportError`` 直接崩掉 —— 但旧名字指向的都是上面这些安全实现。
"""

from __future__ import annotations

import subprocess

from .. import autostart, diagnostics, paths
from ..config import load_state
from ..version import TASK_NAME  # noqa: F401  (保留常量名，避免旧代码 ImportError)

ROOT = paths.install_dir()
LOG_FILE = paths.log_file()
STATE_FILE = paths.state_path()

KEEPALIVE_TASK = TASK_NAME + "-KeepAlive"  # 旧版计划任务名，仅用于清理
TASK_NAMES = (TASK_NAME, KEEPALIVE_TASK)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def decode_console(raw: bytes) -> str:
    """Windows PowerShell 5.1 输出 GBK，PowerShell 7 输出 UTF-8，都要能认。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def powershell_exe() -> str:
    import shutil

    for name in ("powershell", "pwsh"):
        exe = shutil.which(name)
        if exe:
            return exe
    return "powershell"


def pythonw_path() -> str:
    """定位无控制台窗口的解释器（打包后返回自身 EXE）。"""
    return paths.pythonw_path()


# ---------- 自启动（唯一机制：HKCU Run） ----------

def install_task(script_dir=None) -> tuple[bool, str]:
    """兼容旧名字：现在等价于"开启开机自动启动"。"""
    return autostart.enable()


def uninstall_task(script_dir=None) -> tuple[bool, str]:
    """兼容旧名字：关闭自启动，并顺手清理旧版计划任务。"""
    ok, message = autostart.disable()
    cleaned = autostart.cleanup_legacy()
    if cleaned:
        message = message + " " + "；".join(cleaned)
    return ok, message


def task_registered(name: str = TASK_NAME) -> bool:
    """兼容旧名字：是否已设置开机自启动。"""
    return autostart.is_enabled()


def daemon_running() -> bool:
    return autostart.daemon_running()


# ---------- 日志与状态 ----------

def recent_log_lines(count: int = 20) -> list[str]:
    path = paths.log_file()
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-count:]
    except Exception:  # noqa: BLE001
        return []


def last_success_time() -> str:
    try:
        data = load_state()
        ts = int(data.get("last_success_ts") or 0)
        if ts:
            import datetime

            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        return str(data.get("last_strategy") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------- 安全体检（只读，不修改任何安全设置） ----------

def security_status() -> dict:
    """读安全现状：杀毒软件、自启动项、旧版残留、以及"有没有被加过排除项"。"""
    return diagnostics.security_report()


def whitelist_status() -> dict:
    """**已弃用**：V3.0 不再管理杀毒白名单。

    保留这个函数名只为让 V2 的调用点不直接报 ``ImportError``；返回的是
    只读体检结果，字段与旧的 whitelist.ps1 输出兼容的子集。
    """
    report = diagnostics.security_report()
    products = report.get("products") or []
    legacy = report.get("legacy") or {}
    return {
        "ok": False,
        "cancelled": False,
        "admin": False,
        "summary": "V3.0 不再自动添加杀毒白名单；这里只显示现状。",
        "av": list(products),
        "third_party_av": [name for name in products if "Defender" not in name],
        "defender_available": bool(report.get("defender", {}).get("ok")),
        "defender_active": bool(report.get("defender", {}).get("realtime")),
        "defender_error": str(report.get("defender", {}).get("error") or ""),
        "excluded_processes": [],
        "excluded_paths": [],
        "missing_processes": [],
        "missing_paths": [],
        "applied_processes": [],
        "applied_paths": [],
        "errors": [],
        "third_party_hint": "",
        "exclusions_detected": list(report.get("exclusions_hint") or []),
        "legacy": legacy,
    }


def apply_whitelist() -> tuple[bool, str]:
    """**已移除**：V3.0 不提供"一键加白名单"。

    自动把解释器、整个安装目录加进排除项，会扩大安全软件的盲区，属于
    任务书明确禁止的做法。这里保留函数名只为给出清晰提示。
    """
    return False, (
        "V3.0 不再自动添加杀毒软件排除项。如果确实遇到误报，请打开安全软件自己的"
        "防护记录 / 隔离区查看它拦截了哪个文件，再按需手动处理；"
        "也可以在「设置 → 安全体检」里导出脱敏诊断包用于排查。"
    )

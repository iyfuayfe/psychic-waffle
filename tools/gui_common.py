"""GUI 与命令行共用的辅助函数：计划任务、进程状态、日志读取。

放在单独模块里，避免 gui_setup / gui_status / setup_wizard 三处重复实现。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "campus_login.log"
STATE_FILE = ROOT / "state.json"

TASK_NAME = "CampusNetAutoLogin"
KEEPALIVE_TASK = TASK_NAME + "-KeepAlive"
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
    for name in ("powershell", "pwsh"):
        exe = shutil.which(name)
        if exe:
            return exe
    return "powershell"


def pythonw_path() -> str:
    """定位 pythonw.exe（无控制台窗口的解释器）。"""
    exe = shutil.which("pythonw.exe") or shutil.which("pythonw")
    if exe:
        return exe
    exe = shutil.which("python.exe")
    if exe:
        cand = Path(exe).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    if getattr(sys, "executable", ""):
        cand = Path(sys.executable).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return ""


# ---------- 计划任务 ----------

def install_task(script_dir: Path | None = None) -> tuple[bool, str]:
    ps1 = (script_dir or (ROOT / "tools")) / "install_task.ps1"
    if not ps1.exists():
        return False, f"找不到 {ps1}"
    try:
        proc = subprocess.run(
            [powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, timeout=180, creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"调用失败：{exc}"
    return proc.returncode == 0, decode_console((proc.stdout or b"") + (proc.stderr or b"")).strip()


def uninstall_task(script_dir: Path | None = None) -> tuple[bool, str]:
    ps1 = (script_dir or (ROOT / "tools")) / "uninstall_task.ps1"
    if not ps1.exists():
        return False, f"找不到 {ps1}"
    try:
        proc = subprocess.run(
            [powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, timeout=180, creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"调用失败：{exc}"
    return proc.returncode == 0, decode_console((proc.stdout or b"") + (proc.stderr or b"")).strip()


def task_registered(name: str = TASK_NAME) -> bool:
    try:
        proc = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", name],
            capture_output=True, timeout=20, creationflags=_NO_WINDOW,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------- 后台进程 ----------

def daemon_running() -> bool:
    """用命名互斥体判断后台守护是否在跑 —— 比找进程更准，也不会误判。

    守护进程持有一个名为 Local\\CampusNetAutoLogin 的互斥体；
    如果我们能拿到它，说明没人在跑。
    """
    try:
        from campusnet import crypto

        handle = crypto.acquire_single_instance()
        if handle is None:
            return True
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))
        except Exception:  # noqa: BLE001
            pass
        return False
    except Exception:  # noqa: BLE001
        return False


# ---------- 日志与状态 ----------

def recent_log_lines(count: int = 20) -> list[str]:
    try:
        if not LOG_FILE.exists():
            return []
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-count:]
    except Exception:  # noqa: BLE001
        return []


def last_success_time() -> str:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            ts = int(data.get("last_success_ts") or 0)
            if ts:
                import datetime

                return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            return str(data.get("last_strategy") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------- 杀毒软件白名单 ----------

WL_PS1 = ROOT / "tools" / "whitelist.ps1"

_WL_LIST_KEYS = (
    "av", "third_party_av", "excluded_processes", "excluded_paths",
    "missing_processes", "missing_paths", "applied_processes",
    "applied_paths", "errors",
)


def _wl_result_file(tag: str) -> Path:
    return Path(tempfile.gettempdir()) / f"campusnet_wl_{tag}_{os.getpid()}.json"


def _wl_empty() -> dict:
    empty: dict = {k: [] for k in _WL_LIST_KEYS}
    empty.update({
        "ok": False, "cancelled": False, "admin": False, "summary": "",
        "defender_available": False, "defender_active": False,
        "defender_error": "", "third_party_hint": "",
    })
    return empty


def _wl_parse(result_file: Path) -> dict | None:
    """读 whitelist.ps1 写出的 UTF-8 JSON，并把单元素数组还原成 list。"""
    try:
        data = json.loads(result_file.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    for key in _WL_LIST_KEYS:
        val = data.get(key)
        if isinstance(val, str):
            data[key] = [val] if val else []
        elif not isinstance(val, list):
            data[key] = []
    return data


def _wl_run(args: list[str], tag: str, timeout: int) -> tuple[bool, str]:
    """跑 whitelist.ps1 并返回 (是否成功, 摘要)。结果通过 ResultFile 传回。"""
    ps1 = WL_PS1
    if not ps1.exists():
        return False, f"找不到 {ps1}"
    result_file = _wl_result_file(tag)
    cmd = [
        powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(ps1), *args, "-ResultFile", str(result_file),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "超时：白名单操作没在预期时间内完成"
    except Exception as exc:  # noqa: BLE001
        return False, f"调用失败：{exc}"

    data = _wl_parse(result_file)
    try:
        result_file.unlink()
    except Exception:  # noqa: BLE001
        pass
    fallback = decode_console((proc.stdout or b"") + (proc.stderr or b"")).strip()
    if data is None:
        return proc.returncode == 0, fallback or "（没有返回结果）"
    return bool(data.get("ok")), data.get("summary") or fallback


def whitelist_status() -> dict:
    """只读查询杀毒白名单现状（不需要管理员）。失败时返回空骨架 dict。"""
    ps1 = WL_PS1
    if not ps1.exists():
        return _wl_empty()
    result_file = _wl_result_file("check")
    try:
        subprocess.run(
            [powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-Check", "-ResultFile", str(result_file)],
            capture_output=True, timeout=90, creationflags=_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        return _wl_empty()
    data = _wl_parse(result_file)
    try:
        result_file.unlink()
    except Exception:  # noqa: BLE001
        pass
    return data if data is not None else _wl_empty()


def apply_whitelist() -> tuple[bool, str]:
    """把程序加入杀毒白名单。会请求管理员权限（弹 UAC，请点「是」）。"""
    return _wl_run(["-Apply"], "apply", timeout=600)

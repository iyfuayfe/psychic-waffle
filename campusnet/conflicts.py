"""检测会注入本进程的第三方"叠加类"软件。

**为什么需要这个模块**（真实事故）：

某台机器上双击 ``CampusNet.exe``，窗口出现 3 秒后**整个进程消失**，既没有
Python 报错也没有我们自己的日志。Windows 事件日志给出真相::

    错误应用程序名称: CampusNet.exe, 版本: 3.0.0.0
    错误模块名称: GPP64.dll, 版本: 3.7.57.625
    异常代码: 0xc0000005（访问冲突）

``GPP64.dll`` 来自游戏加加（GamePP）的注入 SDK。为了确认不是本程序的 bug，
用一份**只有 20 行**的最小 tkinter 程序做对照：它同样在第 3 秒崩溃、同样报
``GPP64.dll``；把游戏加加退出后再运行，我们的 GUI 稳定存活 25 秒以上。

结论：**这是第三方注入 DLL 与 tkinter/GDI 的冲突**，任何 Tk 桌面程序在这台机器上
都会闪退。程序自己能做的、也确实有用的事是：

1. 启动时就查出这些注入模块（用 Toolhelp32 枚举本进程模块，纯 ctypes）；
2. 写进日志、写进「安全体检 / 诊断包」，并在界面上直接提示**怎么解决**；
3. 后台守护不创建任何窗口，不受影响 —— 这一点也要明确告诉用户。

注意：这里只**读**模块列表，不会尝试卸载、屏蔽或修改第三方软件。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from dataclasses import dataclass

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010


@dataclass
class Conflict:
    """一个可能与本程序冲突的注入模块。"""

    module: str
    path: str = ""
    vendor: str = ""
    advice: str = ""
    verified: bool = False   # 是否已在真实环境确认会导致崩溃

    def summary(self) -> str:
        return f"{self.module}（{self.vendor or '来源未知'}）"


# 已知会向 GUI 进程注入、并可能导致闪退的叠加类模块。
# 只写我们**有依据**说明的条目；不确定的不列，避免误导用户去关掉正常软件。
KNOWN_OVERLAYS = {
    # 已实测：游戏加加注入的 GPP64.dll 会让任意 tkinter 程序在 3 秒左右崩溃
    "gpp64.dll": (
        "游戏加加（GamePP）",
        "退出游戏加加后重试；或在游戏加加的设置里把 CampusNet.exe 加入排除/忽略列表。",
        True,
    ),
    "gpp.dll": (
        "游戏加加（GamePP）",
        "同上：退出游戏加加，或把 CampusNet.exe 加入它的排除列表。",
        True,
    ),
    # 以下为业界常见的游戏内叠加/监控注入，可能与 GDI 窗口冲突
    "rtsshooks64.dll": (
        "RivaTuner / MSI Afterburner 的 OSD",
        "关闭 RivaTuner Statistics Server 的「应用检测/OSD」后重试。",
        False,
    ),
    "rtsshooks.dll": ("RivaTuner Statistics Server 的 OSD", "关闭 RTSS 后重试。", False),
    "discordhook64.dll": (
        "Discord 游戏内叠加",
        "在 Discord 设置 → 游戏活动 → 关闭「游戏内叠加」后重试。",
        False,
    ),
    "nahimicosd.dll": (
        "Nahimic 音频叠加",
        "在 Nahimic 设置里关闭 OSD，或临时退出 Nahimic 服务。",
        False,
    ),
    "nahimic2osd.dll": ("Nahimic 音频叠加", "在 Nahimic 设置里关闭 OSD。", False),
    "fpsmon.dll": ("FPS Monitor 叠加", "退出 FPS Monitor 后重试。", False),
    "overlay64.dll": ("某款游戏叠加组件", "逐个退出叠加类软件后重试。", False),
}


class _ModuleEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", wt.WCHAR * 256),
        ("szExePath", wt.WCHAR * 260),
    ]


def loaded_modules() -> list[tuple[str, str]]:
    """枚举**当前进程**已加载的模块，返回 [(模块名, 完整路径)]。

    纯 ctypes + Toolhelp32，不依赖 psutil / pywin32。非 Windows 或调用失败时
    返回空列表（调用方不需要额外判断）。
    """
    if sys.platform != "win32":
        return []
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:  # noqa: BLE001
        return []

    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_ModuleEntry32W)]
    kernel32.Module32FirstW.restype = wt.BOOL
    kernel32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_ModuleEntry32W)]
    kernel32.Module32NextW.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, ctypes.windll.kernel32.GetCurrentProcessId()
    )
    if not snapshot or snapshot == wt.HANDLE(-1).value:
        return []
    try:
        entry = _ModuleEntry32W()
        entry.dwSize = ctypes.sizeof(_ModuleEntry32W)
        found: list[tuple[str, str]] = []
        if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            found.append((str(entry.szModule), str(entry.szExePath)))
            if not kernel32.Module32NextW(snapshot, ctypes.byref(entry)):
                break
        return found
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:  # noqa: BLE001
            pass


def detect() -> list[Conflict]:
    """返回当前进程里命中的已知冲突模块（保持已知表的顺序）。"""
    modules = {name.lower(): path for name, path in loaded_modules() if name}
    hits: list[Conflict] = []
    for key, (vendor, advice, verified) in KNOWN_OVERLAYS.items():
        if key in modules:
            hits.append(
                Conflict(
                    module=key,
                    path=modules.get(key, ""),
                    vendor=vendor,
                    advice=advice,
                    verified=verified,
                )
            )
    return hits


def describe(hits: list[Conflict] | None = None) -> str:
    """把检测结果说成一句人话（日志 / 界面 / 诊断包共用）。"""
    hits = detect() if hits is None else hits
    if not hits:
        return ""
    names = "、".join(hit.summary() for hit in hits)
    verified = [hit for hit in hits if hit.verified]
    prefix = "检测到会注入本程序的第三方叠加软件：" + names
    if verified:
        prefix += "。其中已确认会导致界面闪退"
    advice = hits[0].advice
    return (
        f"{prefix}。请{advice}"
        "（本程序的后台自动认证不创建窗口，不受影响；"
        "如果界面闪退，可以先用 CampusNetCLI.exe status 查看状态。）"
    )


def has_crash_risk() -> bool:
    """是否存在"已确认会导致闪退"的注入模块。"""
    return any(hit.verified for hit in detect())


def overlay_present() -> bool:
    """是否存在任何已知叠加类注入模块（不区分是否已确认）。"""
    return bool(detect())


# 已知叠加软件的**进程名**（有些工具在看到 GUI 窗口之前还没注入本进程，
# 但它的主程序已经在跑了 —— 用进程名判断能更早发现"这台机器有这个风险"）
OVERLAY_PROCESSES = {
    "gamepp.exe": "游戏加加（GamePP）",
    "rtss.exe": "RivaTuner Statistics Server",
    "msiafterburner.exe": "MSI Afterburner",
    "fpsmon.exe": "FPS Monitor",
    "nahimic.exe": "Nahimic",
}


def overlay_processes_running() -> list[tuple[str, str]]:
    """返回正在运行的已知叠加软件 [(进程名, 厂商)]。只读，失败返回空列表。"""
    if sys.platform != "win32":
        return []
    import subprocess

    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        return []
    text = (proc.stdout or b"").decode("mbcs", "replace").lower()
    found: list[tuple[str, str]] = []
    for name, vendor in OVERLAY_PROCESSES.items():
        if name in text:
            found.append((name, vendor))
    return found


def environment_risk() -> str:
    """这台机器上是否存在"会让本程序界面闪退"的风险，返回可读描述（无风险返回空串）。

    给自动化测试用：在装了游戏加加的机器上，GUI 存活检查失败应该被判为
    "环境不适用（SKIP）"，而不是"产品缺陷（FAIL）"。
    """
    hits = detect()
    if hits:
        return describe(hits)
    processes = overlay_processes_running()
    if processes:
        names = "、".join(vendor for _n, vendor in processes)
        return (
            f"这台机器正在运行 {names}。这类叠加软件会向 GUI 进程注入 DLL，"
            "已知（游戏加加）会导致任何 tkinter 窗口在数秒内崩溃，"
            "因此界面的存活检查在此环境下不可靠。"
        )
    return ""

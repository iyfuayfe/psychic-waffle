"""运行会话标记：让"上次界面异常退出"这件事在下一次启动时说得清楚。

用途（真实场景）：

某台机器上 GUI 被第三方注入的 DLL 打崩，进程直接消失 —— 没有 Python 异常、
没有我们自己的日志、用户只看到"窗口自己关了"。下次启动时如果能明确说
"上次界面在 2026-10-19 17:34 异常退出，当时检测到 GPP64.dll"，用户和排障的人
就不用猜了。

做法很简单：GUI 启动时写 ``runtime/gui-session.json``，正常关闭时删掉。
下次启动若发现它还在（而且那个 pid 已经不在了），就说明上次是异常退出。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import conflicts, paths

SESSION_FILE = "gui-session.json"


def session_path() -> Path:
    return paths.runtime_dir() / SESSION_FILE


def begin(kind: str = "gui", extra: dict | None = None) -> Path:
    """标记"开始运行"。返回标记文件路径（失败也不影响启动）。"""
    data = {
        "kind": kind,
        "pid": os.getpid(),
        "started_at": int(time.time()),
        "started_text": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        from .version import __version__

        data["version"] = __version__
    except Exception:  # noqa: BLE001
        pass
    try:
        hits = conflicts.detect()
        if hits:
            data["conflicts"] = [hit.module for hit in hits]
            data["conflict_vendor"] = [hit.vendor for hit in hits]
    except Exception:  # noqa: BLE001
        pass
    if extra:
        data.update(extra)

    path = session_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return path


def end() -> None:
    """正常结束：删掉标记。"""
    try:
        session_path().unlink()
    except Exception:  # noqa: BLE001
        pass


def refresh(extra: dict | None = None) -> None:
    """更新已有标记的内容（例如运行中途才检测到注入模块）。

    必须在"已经开始会话"后调用；标记不存在时不做任何事，避免在没跑界面的
    情况下凭空造出一个"上次异常退出"。
    """
    path = session_path()
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        if extra:
            data.update(extra)
        data["updated_at"] = int(time.time())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def previous() -> dict | None:
    """读取上一次的会话标记；进程已不存在时返回其内容（调用方负责清理）。

    返回 ``None`` 表示上次是正常退出（或没有记录）。
    """
    path = session_path()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None

    pid = int(data.get("pid") or 0)
    if pid and _pid_alive(pid):
        # 同一个 pid 还活着：多半是标志文件被复用，不要误报
        return None
    return data


def take_previous() -> dict | None:
    """读取并**消费**上一次的异常退出记录。

    为什么必须"先读再写"：新会话一开始就会把自己的标记写进去，如果先
    ``begin()`` 再 ``previous()``，读到的其实是新标记，上一次的线索就被自己
    抹掉了（第一版就是这么写的，实测永远报不出"上次异常退出"）。
    """
    data = previous()
    try:
        session_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return data


def describe_previous(data: dict | None) -> str:
    """把上一次异常退出说成一句人话。"""
    if not data:
        return ""
    when = data.get("started_text") or ""
    modules = data.get("conflicts") or []
    text = f"上次界面在 {when} 异常退出（不是正常关闭）"
    if modules:
        vendors = data.get("conflict_vendor") or []
        label = "、".join(
            f"{module}（{vendors[index]}）" if index < len(vendors) else module
            for index, module in enumerate(modules)
        )
        text += f"，当时检测到注入模块：{label}"
    return text + "。详细原因见日志与 CampusNetCLI.exe doctor 的诊断包。"


def _pid_alive(pid: int) -> bool:
    """尽力判断 pid 是否还活着（Windows 上用 OpenProcess）。"""
    try:
        if os.name != "nt":
            return False
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return True
            STILL_ACTIVE = 259
            return exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False

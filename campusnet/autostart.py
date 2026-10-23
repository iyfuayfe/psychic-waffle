"""开机自启动的唯一机制 + 后台进程的启停。

任务书要求"如果双重自启动机制没有明显必要，应优先简化为一种清晰可靠的方案"。
V2 同时用了**计划任务**和**启动文件夹 VBS**，两者都会拉起后台进程；实测主要
问题是：

* 计划任务注册 ``ONLOGON`` 通常需要管理员权限，普通用户装不上；
* 两份自启动互相不知道对方，靠互斥体兜底，出问题时很难解释"到底谁拉起来的"；
* 启动文件夹里的 VBS 还写死了 Python 绝对路径，换机器直接失效。

V3.0 的结论：**只用 HKCU 的 Run 键**，理由如下。

1. 纯用户级，不需要管理员权限，也不需要 UAC；
2. 删起来干净（一个注册表值），卸载可以明确告诉用户动过什么；
3. 打包成 EXE 后命令行就是 ``"CampusNetService.exe" --daemon``，不依赖 Python；
4. 单实例互斥体保证"登录时重复触发"也只有一个后台进程；
5. 守护进程内部是"永不死"的循环（所有异常就地捕获 + 退避重试），
   不依赖计划任务做自愈。GUI 也会读心跳，发现没在跑可以一键拉起。

旧版留下的计划任务与启动文件夹脚本由 :func:`cleanup_legacy` 清理，
**只删本程序自己创建的那几个名字**，不动其它任何启动项。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import crypto, paths
from .version import mutex_name

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CampusNetDaemon"

# 旧版留下的痕迹（只清理这些精确名字）
LEGACY_TASKS = ("CampusNetAutoLogin", "CampusNetAutoLogin-KeepAlive")
LEGACY_STARTUP_SCRIPTS = ("campus-net-autologin.vbs",)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

STOP_FLAG_NAME = "stop.flag"
STOP_TIMEOUT = 20.0


@dataclass
class AutostartStatus:
    enabled: bool = False
    command: str = ""
    expected: str = ""
    stale: bool = False          # 注册的命令和当前程序位置不一致（搬迁/重装过）
    error: str = ""
    daemon_running: bool = False
    mechanism: str = "HKCU Run"


# ---------------------------------------------------------------- 命令行

def autostart_args() -> list:
    """自启动时执行的完整命令行。"""
    return paths.daemon_command()


def command_string() -> str:
    """Windows Run 键里登记的字符串（路径带空格必须加引号）。"""
    return subprocess.list2cmdline(autostart_args())


# ---------------------------------------------------------------- 注册表

def _winreg():
    import winreg  # noqa: PLC0415  仅 Windows 可用，延迟导入便于在其它平台测试

    return winreg


def registered_command() -> str:
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value or "")
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001
        return ""


def is_enabled() -> bool:
    return bool(registered_command())


def enable() -> tuple[bool, str]:
    """登记开机自启动（只写 HKCU，无需管理员）。"""
    command = command_string()
    try:
        winreg = _winreg()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法写入开机自启动项：{exc}"
    return True, f"已设为开机自动启动（{command}）"


def disable() -> tuple[bool, str]:
    """删除开机自启动项（只动本程序自己的值）。"""
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, "开机自启动本来就是关闭的。"
    except Exception as exc:  # noqa: BLE001
        return False, f"无法删除开机自启动项：{exc}"
    return True, "已取消开机自动启动。"


# ---------------------------------------------------------------- 后台进程

def daemon_running() -> bool:
    """用命名互斥体判断后台守护是否在跑。

    只做探测，拿到句柄后**必须立刻关闭**：否则探测方自己就成了"持有互斥体的
    进程"，真正要启动的守护反而抢不到锁（实测踩过：连续探测几十次后守护起不来）。
    """
    handle = None
    try:
        handle = crypto.acquire_single_instance(mutex_name(paths.data_dir_key()))
        return handle is None
    except Exception:  # noqa: BLE001
        return False
    finally:
        crypto.release_single_instance(handle)


def start_daemon() -> tuple[bool, str]:
    """立刻拉起后台守护（已存在则直接返回成功）。

    成功判定**不只看"进程抢到了锁"**：还要确认它真的写出了心跳（说明它已经跑起来
    并进到了主循环）。如果锁被持有但心跳一直不出现，说明进程卡在早期阶段
    （例如数据目录不可写），这时要如实告诉用户，而不是报"已启动"。

    失败时**必须给出可读的原因**：后台进程可能因为"密码解不开""配置不完整"
    直接退出。只回一句"没有按时就绪"对用户毫无帮助，所以这里会读它自己写的
    日志尾部并把最关键的几行带回来。
    """
    stale_note = False
    if daemon_running():
        # 互斥体是命名对象：可能被"属于另一个数据目录的实例"或异常退出的残留持有。
        # 只有当前数据目录里存在**新鲜心跳**，才能确定是本程序在正常工作；
        # 否则不在这里武断失败，而是继续尝试启动，由后面的循环据实报告结果。
        from .config import load_heartbeat

        beat = load_heartbeat()
        stamp = int(beat.get("updated_at") or 0)
        age = time.time() - stamp if stamp else -1.0
        if age >= 0 and age <= 180:
            return True, "后台守护已经在运行。"
        stale_note = True

    # 清掉上一轮遗留的"停止标记"。
    # 真实故障：用户点过「停止」后进程被强杀/崩溃，stop.flag 会留在数据目录里；
    # 下次启动守护时它会立刻看到这个标记并"优雅退出"（退出码 0、不写心跳），
    # 表现就是"守护启动不起来、自动连接不生效"。所以启动前先清理。
    stale_flag = stop_flag_path()
    try:
        if stale_flag.exists():
            stale_flag.unlink()
            try:
                from .logs import get_logger

                get_logger().warning("检测到上次遗留的停止标记，已清理后再启动后台守护。")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001  清不掉就继续，让守护自己处理
        pass

    # 启动前记下当前心跳，避免把上一轮的旧心跳当成"这次启动成功"的证据
    from .config import load_heartbeat

    before = load_heartbeat()
    before_stamp = int(before.get("updated_at") or 0)

    args = paths.daemon_command()
    log_path = paths.log_file()
    log_size_before = _file_size(log_path)
    try:
        process = subprocess.Popen(  # noqa: S603
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_NO_WINDOW | _DETACHED,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"无法启动后台守护：{exc}"

    deadline = time.time() + max(6.0, START_TIMEOUT)
    locked_at = 0.0
    while time.time() < deadline:
        time.sleep(0.15)
        if daemon_running():
            locked_at = locked_at or time.time()
            # 进程已抢到锁：再给它一点时间写出心跳，确认它真的在工作
            beat = load_heartbeat()
            if int(beat.get("updated_at") or 0) > before_stamp:
                if stale_note:
                    return True, (
                        "后台守护已启动（此前有一个旧实例占着运行锁，现已由本实例接管）。"
                    )
                return True, "后台守护已启动。"
            if time.time() - locked_at > 5.0:
                if stale_note:
                    return False, (
                        "运行锁被另一个实例占用，且它在当前数据目录里没有心跳"
                        "（可能是另一个数据目录或旧版本遗留的实例）。"
                        "可执行 CampusNetCLI.exe stop 停止它，或结束 CampusNetService 进程后重试。"
                    )
                return True, (
                    "后台守护已启动，但还没有写出心跳（可能数据目录不可写）。"
                    f"请在界面上查看状态，或检查日志：{log_path}"
                )
            continue
        exit_code = process.poll()
        if exit_code is not None:
            # 守护自己退出了：把它的日志原因翻出来
            reason = _daemon_exit_reason(log_path, log_size_before, exit_code)
            return False, reason

    # 进程还在但没抢到互斥体 —— 少见，保留原文案但补上排查方向
    if stale_note:
        return False, (
            "运行锁仍被另一个实例占用（当前数据目录没有心跳）。"
            "可执行 CampusNetCLI.exe stop 停止它后重试。"
        )
    return False, (
        "后台守护启动后没有按时就绪。"
        f"请查看日志：{log_path}"
    )


START_TIMEOUT = 20.0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:  # noqa: BLE001
        return 0


def _daemon_exit_reason(log_path: Path, size_before: int, exit_code: int) -> str:
    """把"守护刚启动就退出"的原因翻译成用户能照着做的提示。"""
    new_lines = ""
    try:
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                if size_before and log_path.stat().st_size >= size_before:
                    handle.seek(size_before)
                new_lines = handle.read()[-4000:]
    except Exception:  # noqa: BLE001
        new_lines = ""

    # 退出码含义与 campusnet/daemon.py 保持一致：3=缺凭据/解密失败，4=配置读不了
    if "密码解密失败" in new_lines:
        return (
            "后台守护启动失败：配置里的密码在本机解不开（换了 Windows 用户或电脑后会出现）。"
            "请在「设置」里重新输入一次上网密码并保存。"
        )
    if "配置不完整" in new_lines:
        return (
            "后台守护启动失败：配置里缺少账号或密码。"
            "请在「设置」里填写学号 / 上网账号与上网密码并保存。"
        )
    if exit_code == 3:
        return (
            "后台守护启动失败：账号或密码不可用（可能未保存，或密码在本机解不开）。"
            "请在「设置」里重新填写并保存。"
        )
    if exit_code == 4:
        return "后台守护启动失败：配置文件无法解析。请打开「设置」重新保存一次配置。"
    tail = " ".join(line.strip() for line in new_lines.strip().splitlines()[-3:])
    suffix = f"（守护输出：{tail[:200]}）" if tail else ""
    return f"后台守护启动后立即退出（退出码 {exit_code}）{suffix}"


def stop_flag_path() -> Path:
    return paths.data_dir() / STOP_FLAG_NAME


def request_stop() -> tuple[bool, str]:
    """给守护进程写一个"请优雅退出"的标记文件。"""
    try:
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        stop_flag_path().write_text(str(int(time.time())), encoding="utf-8")
        return True, "已请求后台守护退出。"
    except Exception as exc:  # noqa: BLE001
        return False, f"无法写入停止标记：{exc}"


def clear_stop_flag() -> None:
    try:
        stop_flag_path().unlink()
    except Exception:  # noqa: BLE001
        pass


def stop_daemon(timeout: float = STOP_TIMEOUT) -> tuple[bool, str]:
    """优雅停止后台守护：写标记 → 等互斥体释放 → 清标记。"""
    if not daemon_running():
        clear_stop_flag()
        return True, "后台守护没有在运行。"
    ok, message = request_stop()
    if not ok:
        return False, message
    deadline = time.time() + max(3.0, timeout)
    while time.time() < deadline:
        if not daemon_running():
            clear_stop_flag()
            return True, "后台守护已停止。"
        time.sleep(0.2)
    # 超时：留着标记没有意义（守护下次启动会立刻看到它并直接退出），这里清掉并说明
    clear_stop_flag()
    return False, (
        "后台守护没有在预期时间内退出（可能正在等待网络请求）。"
        "已清理停止标记，稍后重试；也可在任务管理器里结束 CampusNetService。"
    )


# ---------------------------------------------------------------- 旧版清理

def cleanup_legacy() -> list:
    """清理 V2 留下的计划任务与启动文件夹脚本。

    返回"做了什么"的文字列表，供卸载流程原样打印给用户；只删除本程序
    自己创建过的精确名字，失败也照样继续。
    """
    done: list = []

    for name in LEGACY_TASKS:
        try:
            query = subprocess.run(["schtasks.exe", "/Query", "/TN", name],
                                   capture_output=True, timeout=20, creationflags=_NO_WINDOW)
            if query.returncode == 0:
                delete = subprocess.run(["schtasks.exe", "/Delete", "/TN", name, "/F"],
                                        capture_output=True, timeout=20, creationflags=_NO_WINDOW)
                if delete.returncode == 0:
                    done.append(f"已删除旧版计划任务：{name}")
                else:
                    done.append(f"旧版计划任务删除失败（不影响使用）：{name}")
        except Exception as exc:  # noqa: BLE001
            done.append(f"检查旧版计划任务 {name} 失败：{exc}")

    startup_dir = _startup_dir()
    if startup_dir is not None:
        for script in LEGACY_STARTUP_SCRIPTS:
            target = startup_dir / script
            if target.exists():
                try:
                    target.unlink()
                    done.append(f"已删除旧版启动脚本：{script}")
                except Exception as exc:  # noqa: BLE001
                    done.append(f"旧版启动脚本删除失败：{script}（{exc}）")
    return done


def legacy_present() -> dict:
    """旧版残留清单（只读，给界面提示用）。"""
    found = {"tasks": [], "startup_scripts": []}
    for name in LEGACY_TASKS:
        try:
            query = subprocess.run(["schtasks.exe", "/Query", "/TN", name],
                                   capture_output=True, timeout=15, creationflags=_NO_WINDOW)
            if query.returncode == 0:
                found["tasks"].append(name)
        except Exception:  # noqa: BLE001
            continue
    startup_dir = _startup_dir()
    if startup_dir is not None:
        for script in LEGACY_STARTUP_SCRIPTS:
            if (startup_dir / script).exists():
                found["startup_scripts"].append(script)
    return found


def _startup_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


# ---------------------------------------------------------------- 汇总

def status() -> AutostartStatus:
    info = AutostartStatus()
    info.expected = command_string()
    try:
        current = registered_command()
    except Exception as exc:  # noqa: BLE001
        info.error = str(exc)
        current = ""
    info.command = current
    info.enabled = bool(current)
    info.stale = bool(current) and current.strip().lower() != info.expected.strip().lower()
    info.daemon_running = daemon_running()
    return info


def status_dict() -> dict:
    info = status()
    payload = {
        "enabled": info.enabled,
        "daemon_running": info.daemon_running,
        "command": info.command,
        "expected": info.expected,
        "stale": info.stale,
        "error": info.error,
        "mechanism": info.mechanism,
    }
    # 真实健康状态（进程 + 心跳新鲜度 + 状态），供界面判断"是不是真的在工作"
    try:
        from .daemon import daemon_health

        payload["health"] = daemon_health().as_dict()
    except Exception as exc:  # noqa: BLE001  健康探测失败不能拖垮状态查询
        payload["health"] = {
            "running": info.daemon_running,
            "healthy": False,
            "label": "状态未知",
            "reason": f"健康探测失败：{exc}",
            "needs_attention": False,
        }
    return payload

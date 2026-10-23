"""路径策略：安装目录只读，用户数据统一放在 %LOCALAPPDATA%\\CampusNet。

为什么必须分开（V3.0 的核心改动之一）：

* 打包成 EXE 后程序通常装在 ``C:\\Program Files`` 或类似位置，普通用户对安装目录
  没有写权限；把 ``config.json`` / ``logs/`` 继续放在程序目录会导致"配置保存失败"
  或"日志写不出来"。
* 反过来，源码模式的旧用户已经把配置放在项目根目录，直接搬到新位置会让他们的账号
  密码"凭空消失"。所以升级时**复制**旧文件到新位置（旧文件原地保留），并记一份迁移
  报告，失败可以随时回退。

目录一览（``frozen`` = 打包后以 sys.frozen 判定）::

    安装目录（只读）      <exe 所在目录>   或 项目根目录（源码模式）
    资源目录（只读）      _MEIPASS（onefile）或 安装目录
    用户数据目录          %LOCALAPPDATA%\\CampusNet
        config.json       配置（含 DPAPI 密文）
        logs\\            运行日志
        state.json        运行状态
        backup\\          旧配置备份 / 迁移备份
        tmp\\             浏览器临时用户目录等
        diagnostics\\     --doctor 生成的脱敏诊断包

可用环境变量 ``CAMPUSNET_DATA_DIR`` 覆盖数据目录（测试、便携模式、多账号隔离都靠它）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .version import DATA_DIR_NAME

_ENV_DATA_DIR = "CAMPUSNET_DATA_DIR"

# 旧版把用户数据放在程序目录里，这些名字要参与迁移
LEGACY_CONFIG_NAME = "config.json"
LEGACY_STATE_NAME = "state.json"
LEGACY_LOG_DIR_NAME = "logs"


# ------------------------------------------------------------------ 运行形态

def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的 EXE 里。"""
    return bool(getattr(sys, "frozen", False))


def frozen_exe() -> Path | None:
    """打包后返回自身 EXE 路径；源码模式返回 None。

    注意：打包后 ``sys.executable`` 指向**本程序**，不再是 python.exe，
    所以任何"用 sys.executable 起一个 Python 脚本"的写法都必须先判断这里。
    """
    if not is_frozen():
        return None
    exe = getattr(sys, "executable", "") or ""
    return Path(exe) if exe else None


def install_dir() -> Path:
    """程序安装目录（只读，不写用户数据）。"""
    exe = frozen_exe()
    if exe is not None:
        return exe.resolve().parent
    # 源码模式：campusnet/ 的上一级就是项目根
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """只读资源目录（图标、示例配置等）。onefile 模式下是临时解包目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return install_dir()


def resource(*parts: str) -> Path:
    """拼一个资源文件路径（找不到时退回安装目录）。"""
    candidate = resource_dir().joinpath(*parts)
    if candidate.exists():
        return candidate
    return install_dir().joinpath(*parts)


def is_portable() -> bool:
    """便携模式：安装目录里放了 ``portable.txt`` 就把数据写回程序目录。

    只作为显式选择提供（U 盘场景），默认永远不用；写入失败时自动回落到
    用户数据目录，不会因为便携模式把程序搞坏。
    """
    marker = install_dir() / "portable.txt"
    if not marker.exists():
        return False
    try:
        probe = install_dir() / ".write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 用户数据

def data_dir_key() -> str:
    """数据目录的短指纹（8 位十六进制），用于给全局命名对象（互斥体）做区分。

    同一个数据目录永远得到同一个值；不同数据目录（便携版 / 安装版 / 测试隔离
    目录）互不相同，因此它们的守护进程不会互相"占锁"。
    """
    try:
        raw = str(data_dir()).lower().encode("utf-8", "replace")
        return hashlib.sha1(raw).hexdigest()[:8]  # noqa: S324  (仅作稳定标识)
    except Exception:  # noqa: BLE001
        return ""


def data_dir() -> Path:
    """用户数据根目录。可用环境变量覆盖。"""
    override = (os.environ.get(_ENV_DATA_DIR) or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_portable():
        return install_dir() / "userdata"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / DATA_DIR_NAME
    # 极端兜底（LOCALAPPDATA 缺失）：用户主目录下隐藏目录
    return Path.home() / f".{DATA_DIR_NAME.lower()}"


def config_path() -> Path:
    return data_dir() / "config.json"


def state_path() -> Path:
    return data_dir() / "state.json"


def heartbeat_path() -> Path:
    """后台守护的实时心跳（GUI 用它显示"正在运行 / 最近一次结果"）。"""
    return data_dir() / "heartbeat.json"


def log_dir() -> Path:
    return data_dir() / "logs"


def log_file() -> Path:
    return log_dir() / "campus_login.log"


def backup_dir() -> Path:
    return data_dir() / "backup"


def temp_dir() -> Path:
    return data_dir() / "tmp"


def diagnostics_dir() -> Path:
    return data_dir() / "diagnostics"


def runtime_dir() -> Path:
    """运行期标记（会话标记、崩溃线索等）。正常退出会被清掉或忽略。"""
    return data_dir() / "runtime"


def browser_profile_root() -> Path:
    return temp_dir() / "browser"


def ensure_dirs() -> None:
    """创建所有必须可写的目录（失败时抛出，由调用方决定怎么提示）。"""
    for path in (data_dir(), log_dir(), temp_dir(), browser_profile_root(), runtime_dir()):
        path.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ 旧版布局

def legacy_config_path() -> Path:
    return install_dir() / LEGACY_CONFIG_NAME


def legacy_state_path() -> Path:
    return install_dir() / LEGACY_STATE_NAME


def legacy_log_dir() -> Path:
    return install_dir() / LEGACY_LOG_DIR_NAME


# ------------------------------------------------------------------ 迁移

@dataclass
class MigrationReport:
    """一次数据迁移的结果，用于日志、GUI 提示与回退。"""

    ran: bool = False
    copied: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    backup: str = ""
    legacy_dir: str = ""
    target_dir: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if not self.ran:
            return ""
        parts = []
        if self.copied:
            parts.append("已迁移 " + "、".join(self.copied))
        if self.skipped:
            parts.append("已跳过 " + "、".join(self.skipped))
        if self.errors:
            parts.append("失败 " + "；".join(self.errors))
        if self.backup:
            parts.append(f"备份在 {self.backup}")
        return "；".join(parts)


def _copy_with_backup(src: Path, dst: Path, backup_root: Path) -> str:
    """把 src 复制到 dst；先备份 dst（若已存在）。返回备份路径（没有则空串）。"""
    backup = ""
    if dst.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        saved = backup_root / f"{dst.name}.{int(time.time())}.bak"
        shutil.copy2(dst, saved)
        backup = str(saved)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return backup


def migrate_legacy_data() -> MigrationReport:
    """把旧版放在程序目录里的用户数据复制到新的用户数据目录。

    规则（全部是"复制"，不动旧文件，失败不破坏现有状态）::

        config.json   新位置没有 -> 复制；新位置已有 -> 跳过（绝不覆盖新配置）
        state.json    同上
        logs\\*.log    仅当新位置没有同名文件时复制

    任何一步失败都记进 report.errors，调用方应提示用户但仍继续运行
    （大不了让用户重新填一次密码）。
    """
    report = MigrationReport(
        legacy_dir=str(install_dir()),
        target_dir=str(data_dir()),
    )
    try:
        if data_dir() == install_dir():
            return report  # 便携模式：本来就在一起，无需迁移
        legacy_cfg = legacy_config_path()
        legacy_state = legacy_state_path()
        if not legacy_cfg.exists() and not legacy_state.exists():
            return report
        if not data_dir().exists() and not _can_create(data_dir()):
            report.errors.append(f"无法创建用户数据目录 {data_dir()}")
            return report

        report.ran = True
        backup_root = backup_dir() / f"migrate-{time.strftime('%Y%m%d-%H%M%S')}"

        for src, dst, label in (
            (legacy_cfg, config_path(), "config.json"),
            (legacy_state, state_path(), "state.json"),
        ):
            if not src.exists():
                continue
            if dst.exists():
                report.skipped.append(f"{label}（目标已存在）")
                continue
            try:
                backup = _copy_with_backup(src, dst, backup_root)
                report.copied.append(label)
                if backup and not report.backup:
                    report.backup = backup
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{label}: {exc}")

        legacy_logs = legacy_log_dir()
        if legacy_logs.exists():
            try:
                log_dir().mkdir(parents=True, exist_ok=True)
                for item in legacy_logs.glob("*.log*"):
                    target = log_dir() / item.name
                    if target.exists():
                        report.skipped.append(f"logs/{item.name}")
                        continue
                    shutil.copy2(item, target)
                report.copied.append("logs/（历史日志副本）")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"logs: {exc}")
    except Exception as exc:  # noqa: BLE001  迁移绝不能拦住启动
        report.ran = True
        report.errors.append(str(exc))
    return report


def _can_create(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 子进程

def service_exe() -> list:
    """打包后应该用哪个可执行文件来跑后台守护。

    关键点：打包后 ``sys.executable`` 是**当前正在运行的那个 EXE**。
    从 ``CampusNet.exe``（图形界面）里拉起后台时必须指向同目录的
    ``CampusNetService.exe``，否则会把界面再开一次。

    找不到 CampusNetService.exe 时（改名、onefile、只复制了单个 EXE）退回
    自身并显式带上 ``--role service``，由 entry.py 按角色分派。
    """
    exe = frozen_exe()
    if exe is None:
        return []
    sibling = exe.with_name("CampusNetService.exe")
    if sibling.exists():
        return [str(sibling)]
    return [str(exe), "--role", "service"]


def daemon_command(config: Path | None = None, extra: list | None = None) -> list:
    """启动后台守护的完整命令（源码模式与打包模式通用）。

    打包后是 ``CampusNetService.exe --daemon``；源码模式是
    ``pythonw.exe -m campusnet.app.service --daemon``。
    绝对不要用 ``sys.executable + script.py`` 硬拼：打包后 sys.executable 是 EXE。
    """
    args = ["--daemon"]
    if config:
        args += ["--config", str(config)]
    args += list(extra or [])
    frozen = service_exe()
    if frozen:
        return [*frozen, *args]
    return [pythonw_path(), "-m", "campusnet.app.service", *args]


def pythonw_path() -> str:
    """源码模式下无控制台窗口的解释器路径。"""
    candidate = Path(sys.executable) if sys.executable else None
    if candidate is not None and candidate.name.lower() == "pythonw.exe":
        return str(candidate)
    if candidate is not None:
        sibling = candidate.with_name("pythonw.exe")
        if sibling.exists():
            return str(sibling)
    return sys.executable or "pythonw.exe"


def describe() -> dict:
    """给 GUI / 诊断输出用的路径总览（不含任何凭据）。"""
    return {
        "frozen": is_frozen(),
        "portable": is_portable(),
        "install_dir": str(install_dir()),
        "resource_dir": str(resource_dir()),
        "data_dir": str(data_dir()),
        "config": str(config_path()),
        "logs": str(log_dir()),
        "state": str(state_path()),
        "backup": str(backup_dir()),
        "runtime": str(runtime_dir()),
        "pythonw": pythonw_path(),
    }

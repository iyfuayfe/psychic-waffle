"""卸载：只清理本程序自己创建过的东西，默认保留用户数据。

步骤（每一步失败都继续，最后一起汇报）：

1. 优雅停止后台守护（写 ``stop.flag``，让循环自己退出，不 taskkill 打断请求）；
2. 关闭开机自启动（删 HKCU Run 里我们那一个值）；
3. 清理旧版（V2）留下的计划任务与启动文件夹脚本（只删精确名字）；
4. **可选**删除用户数据目录（配置 / 日志 / 备份），必须显式要求。

明确不做的事：

* 不动杀毒软件的排除项 —— V3.0 从来没有自动添加过，用户自己加的要用户自己判断；
* 不动其它任何启动项、注册表值、目录；
* 不删除安装目录（那是安装程序的职责，且用户可能自己在用）。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import autostart, paths

# 删除数据目录时的重试次数（其它进程可能刚退出、句柄还没释放）
DELETE_RETRIES = 4
DELETE_RETRY_DELAY = 0.5


def close_own_log_handlers() -> None:
    """关掉本进程打开的日志句柄。

    这一条是**必须**的：CLI / 守护进程自己就通过 ``setup_logging()`` 打开了
    ``logs/campus_login.log``。Windows 上被打开的文件删不掉，于是
    ``uninstall --remove-data`` 会在删除日志时拿到
    ``WinError 32（另一个程序正在使用此文件）`` —— 实测确实如此，
    表现为"卸载说删了数据，但 logs 还在，且整次卸载被判失败"。
    """
    for name in ("campusnet", ""):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _force_remove(func, path, _exc_info=None) -> None:
    """删不掉时先去掉只读属性再试一次（rmtree 的 onerror 回调）。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:  # noqa: BLE001
        pass


def remove_data_dir(target: Path) -> tuple[bool, str]:
    """删除用户数据目录，带重试与只读属性修复。返回 (是否成功, 说明)。"""
    if not target.exists():
        return True, "用户数据目录本来就不存在。"
    close_own_log_handlers()
    last_error = ""
    for attempt in range(1, DELETE_RETRIES + 1):
        try:
            shutil.rmtree(target, onerror=_force_remove)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{exc}"
        if not target.exists():
            return True, f"已删除用户数据目录：{target}"
        time.sleep(DELETE_RETRY_DELAY * attempt)
    return False, (
        f"删除用户数据目录失败：{target}（{last_error}）。"
        "常见原因：还有本程序的进程在运行（先运行 CampusNetCLI.exe stop），"
        "或文件正被杀毒/同步软件占用；稍后手动删除该目录即可。"
    )


@dataclass
class UninstallReport:
    actions: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    data_dir: str = ""
    data_removed: bool = False

    def add(self, text: str) -> None:
        self.actions.append(text)

    def fail(self, text: str) -> None:
        self.errors.append(text)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        lines = list(self.actions)
        if self.errors:
            lines.append("以下步骤没有成功（不影响其它清理结果）：")
            lines.extend("  · " + item for item in self.errors)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "actions": list(self.actions),
            "errors": list(self.errors),
            "data_dir": self.data_dir,
            "data_removed": self.data_removed,
        }


def uninstall(*, remove_data: bool = False, stop_timeout: float = 20.0) -> UninstallReport:
    """执行卸载。``remove_data=True`` 时才会删除用户数据目录。"""
    report = UninstallReport(data_dir=str(paths.data_dir()))

    # 1. 停后台
    try:
        ok, message = autostart.stop_daemon(timeout=stop_timeout)
        report.add(message) if ok else report.fail(message)
    except Exception as exc:  # noqa: BLE001
        report.fail(f"停止后台守护失败：{exc}")

    # 2. 关自启动
    try:
        ok, message = autostart.disable()
        report.add(message) if ok else report.fail(message)
    except Exception as exc:  # noqa: BLE001
        report.fail(f"关闭开机自启动失败：{exc}")

    # 3. 清理旧版痕迹
    try:
        for line in autostart.cleanup_legacy():
            report.add(line)
    except Exception as exc:  # noqa: BLE001
        report.fail(f"清理旧版自启动项失败：{exc}")

    # 4. 用户数据（默认保留）
    if remove_data:
        target = paths.data_dir()
        try:
            ok, message = remove_data_dir(target)
            if ok:
                report.data_removed = True
                report.add(message)
            else:
                report.fail(message)
        except Exception as exc:  # noqa: BLE001
            report.fail(f"删除用户数据目录失败：{target}（{exc}）")
    else:
        report.add(
            f"已保留用户配置与日志：{paths.data_dir()}（如需删除，请加 --remove-data）"
        )

    report.add("未修改任何安全软件设置（V3.0 不会自动添加排除项）。")
    return report

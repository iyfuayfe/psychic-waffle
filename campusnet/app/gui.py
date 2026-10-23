"""``CampusNet.exe``：图形界面主程序。

日常使用只需要这一个 EXE：查看网络状态、管理账号、启用/暂停自动连接、
手动认证一次、查看日志、修改设置。关掉窗口不影响后台守护。
"""

from __future__ import annotations

import sys
import traceback

from .. import paths
from .bootstrap import bootstrap

USAGE = """\
CampusNet 图形界面

用法：
  CampusNet.exe                  打开主窗口
  CampusNet.exe --setup          以「配置向导」方式打开（直接进设置页）
  CampusNet.exe --page settings  打开并切到指定页（overview / settings / auth）
  CampusNet.exe --version        显示版本
"""


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg in ("-h", "--help") for arg in argv):
        try:
            sys.stdout.write(USAGE)
        except Exception:  # noqa: BLE001
            pass
        return 0
    if "--version" in argv:
        from ..version import banner

        try:
            sys.stdout.write(banner() + "\n")
        except Exception:  # noqa: BLE001
            pass
        return 0

    try:
        bootstrap()
    except Exception:  # noqa: BLE001
        _write_crash("bootstrap")
        return 1

    try:
        from ..ui.main_window import main as gui_main

        return gui_main(argv)
    except Exception:  # noqa: BLE001  最后一道防线：写日志，不弹系统对话框
        _write_crash("gui")
        return 1


def _write_crash(tag: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.log_dir() / f"crash-{tag}.log", "a", encoding="utf-8") as handle:
            handle.write(traceback.format_exc())
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""PyInstaller 打包用的统一入口（源码模式也能用）。

打包时同一个脚本会被编译成 4 个 EXE，运行期按**自己的文件名**分派，
这样 .spec 里只需要维护一份 Analysis，四个 EXE 共用同一套依赖：

    CampusNet.exe          -> campusnet.app.gui      图形界面（windowed）
    CampusNetSetup.exe     -> campusnet.app.setup    配置向导（windowed）
    CampusNetService.exe   -> campusnet.app.service  后台守护（windowed）
    CampusNetCLI.exe       -> campusnet.app.cli      命令行诊断（console）

源码模式等价用法：

    python entry.py                       # 图形界面
    python entry.py --role service --once  # 只认证一次
    python entry.py --role cli status
"""

from __future__ import annotations

import sys
from pathlib import Path

# 打包后不需要这一句（依赖已经打进去）；源码模式下要能找到 campusnet 包
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ROLE_BY_EXE = {
    "campusnet": "gui",
    "campusnetgui": "gui",
    "campusnetsetup": "setup",
    "campusnetservice": "service",
    "campusnetdaemon": "service",
    "campusnetcli": "cli",
}


def detect_role(argv: list) -> tuple[str, list]:
    """决定以哪种身份运行，并剥掉 ``--role`` 参数。"""
    role = ""
    rest: list = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--role" and index + 1 < len(argv):
            role = argv[index + 1]
            index += 2
            continue
        if item.startswith("--role="):
            role = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    if role:
        return role.lower(), rest

    exe = Path(sys.argv[0] or "").stem.lower()
    return _ROLE_BY_EXE.get(exe, "gui"), rest


def main(argv: list | None = None) -> int:
    role, rest = detect_role(list(sys.argv[1:] if argv is None else argv))
    if role in ("gui", "window"):
        from campusnet.app.gui import main as run
    elif role in ("setup", "wizard"):
        from campusnet.app.setup import main as run
    elif role in ("service", "daemon", "background"):
        from campusnet.app.service import main as run
    elif role in ("cli", "console"):
        from campusnet.app.cli import main as run
    else:
        sys.stderr.write(f"未知的 --role：{role}\n")
        return 2
    return run(rest)


if __name__ == "__main__":
    sys.exit(main())

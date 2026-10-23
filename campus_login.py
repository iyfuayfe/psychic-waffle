"""兼容入口：``python campus_login.py`` / ``pythonw campus_login.py``。

V3.0 起后台守护的正式入口是 ``CampusNetService.exe``（打包后）或
``python -m campusnet.app.service``（源码模式）。这个文件保留原来的调用方式，
让旧计划任务、旧脚本、旧文档里的命令都还能用。

    python campus_login.py --once --verbose

注意：打包成 EXE 之后**不要**再用 ``python xxx.py`` 的方式启动，请直接用
``CampusNetService.exe``——那时系统里根本没有 Python。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _NullStream:
    """pythonw.exe 下 sys.stdout / sys.stderr 是 None，这里统一兜底。"""

    encoding = "utf-8"
    errors = "replace"

    def write(self, _data):  # noqa: D102
        return 0

    def flush(self):  # noqa: D102
        pass

    def isatty(self):  # noqa: D102
        return False

    def close(self):  # noqa: D102
        pass


for _name in ("stdout", "stderr", "__stdout__", "__stderr__"):
    if getattr(sys, _name, None) is None:
        try:
            setattr(sys, _name, _NullStream())
        except Exception:  # noqa: BLE001
            pass


def main(argv: list | None = None) -> int:
    from campusnet.app.service import main as service_main

    return service_main(argv)


def cli() -> None:  # pragma: no cover
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    cli()

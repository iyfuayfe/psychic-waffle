"""入口：校园网后台自动登录。

平时由计划任务用 pythonw.exe 静默启动（无窗口、无弹窗）：
    pythonw.exe campus_login.py

排障时手动跑一次，看控制台输出：
    python campus_login.py --once --verbose
"""

from __future__ import annotations

import os
import sys

# pythonw.exe 下 sys.stdout / sys.stderr 是 None，第三方库或 print 可能因此炸掉。
# 这里统一兜底成空写入器，确保"绝不弹窗、绝不崩溃"。
class _NullStream:
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

if getattr(sys.stdin, "closed", True) and sys.stdin is None:  # pragma: no cover
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from campusnet.daemon import cli  # noqa: E402

if __name__ == "__main__":
    cli()

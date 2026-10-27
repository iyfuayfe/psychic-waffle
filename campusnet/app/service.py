"""``CampusNetService.exe``：后台守护进程入口。

``--daemon``（默认）：常驻运行，断线自动认证。
``--once``：只做一次检查/认证就退出（排障、GUI 的「立即认证」也走这条路径）。
``--check``：只报网络状态，不认证。

这里是"windowed"进程：没有任何窗口，所有输出只写日志文件。
"""

from __future__ import annotations

import sys

from .. import paths
from .bootstrap import bootstrap


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 允许直接写 `CampusNetService.exe --once` 而不用带 --daemon
    args = [arg for arg in argv if arg != "--daemon"]
    console = "--verbose" in args

    cfg, log, _migration = bootstrap(console=console)

    try:
        from ..daemon import main as daemon_main

        return daemon_main(args)
    except SystemExit as exc:  # argparse 的 --help
        return int(getattr(exc, "code", 0) or 0)
    except Exception as exc:  # noqa: BLE001  最后一道防线
        log.exception("后台守护异常退出：%s", exc)
        try:
            paths.log_dir().mkdir(parents=True, exist_ok=True)
            (paths.log_dir() / "crash-service.log").write_text(
                str(exc), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

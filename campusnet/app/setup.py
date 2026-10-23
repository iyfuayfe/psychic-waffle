"""``CampusNetSetup.exe``：配置向导。

只为"第一次用"服务的入口：直接打开界面的「设置」页，把流程缩短成
填 SSID → 填账号密码 → 测试认证 → 保存并开启自动连接。

不单独维护第二套界面：主窗口本身就包含这套设置，单独 EXE 只是换了个
入口和引导文案，这样两个 EXE 的行为永远不会走偏。
"""

from __future__ import annotations

import sys

from .. import paths
from .bootstrap import bootstrap


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap()
    except Exception:  # noqa: BLE001
        return 1

    try:
        from ..ui.main_window import main as gui_main

        # --setup 让界面进入引导模式（默认停在设置页）
        args = ["--setup", *[a for a in argv if a not in ("--setup",)]]
        code = gui_main(args)
    except Exception:  # noqa: BLE001
        try:
            import traceback

            paths.log_dir().mkdir(parents=True, exist_ok=True)
            (paths.log_dir() / "crash-setup.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass
        return 1

    # 配置完就顺手把后台守护拉起来（失败不阻塞，用户可以在界面上重试）
    try:
        from ..autostart import daemon_running, start_daemon

        if not daemon_running():
            start_daemon()
    except Exception:  # noqa: BLE001
        pass
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

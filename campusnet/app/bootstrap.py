"""启动前的统一准备：路径、目录、旧数据迁移、无控制台兜底。

四个入口（GUI / 向导 / 守护 / CLI）都要走一遍 :func:`bootstrap`，避免各自
漏掉某一步导致"打包后行为不一样"。
"""

from __future__ import annotations

import sys

from .. import paths
from ..logs import setup_logging
from ..version import banner


class _NullStream:
    """windowed EXE / pythonw 下 sys.stdout / sys.stderr 是 None。

    有些库（以及我们自己的 print）会在 None 上直接炸掉，这里统一兜底。
    """

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


def patch_std_streams() -> None:
    """兜底 None 流，并保证控制台输出不会因为编码问题崩掉。

    中文 Windows 控制台默认是 GBK(cp936)。如果日志/输出里出现了 GBK 编不出来的
    字符，`print` 会直接抛 `UnicodeEncodeError` —— 一个诊断工具因此崩掉很尴尬。
    这里只把错误处理改成 replace，**不改编码**（改编码反而会让老控制台变乱码）。
    """
    for name in ("stdout", "stderr", "__stdout__", "__stderr__"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, _NullStream())
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            if hasattr(stream, "reconfigure") and getattr(stream, "errors", "") != "replace":
                stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass


def bootstrap(*, console: bool = False):
    """准备运行环境。返回 (cfg, logger, migration_report)。"""
    from ..config import Config, ConfigError

    patch_std_streams()

    # 进程加固是**可选功能**（3.1.0 起默认关闭）：只有在用户显式设置
    # CAMPUSNET_HARDEN=1 时才会施加进程缓解策略。默认调用只记录决策、不改任何
    # 进程安全属性 —— 详细理由见 campusnet/hardening.py 的模块说明。
    hardening_report: dict = {}
    try:
        from .. import hardening

        hardening_report = hardening.early_protect()
    except Exception as exc:  # noqa: BLE001  记录失败原因，但不影响启动
        hardening_report = {"mode": "unknown", "reason": f"检查失败：{exc}"}

    migration = paths.migrate_legacy_data()
    try:
        paths.ensure_dirs()
    except Exception:  # noqa: BLE001  目录建不出来也要尽量跑起来
        pass

    cfg = Config()
    try:
        cfg = Config.load(paths.config_path())
    except ConfigError:
        # 配置坏了：用默认值继续，具体错误留给调用方/日志说明
        cfg = Config()

    log = setup_logging(cfg, console=console)
    log.debug("启动：%s，数据目录 %s", banner(), paths.data_dir())
    mode = hardening_report.get("mode", "")
    if hardening_report.get("extension_points") == "ok":
        log.info("进程加固（可选，用户已开启）：已禁用扩展点注入")
    elif mode == "opt-in":
        log.warning("进程加固（可选）启用但未生效：%s", hardening_report.get("reason", ""))
    else:
        # 默认路径：明确记录"没有施加任何进程缓解策略"，便于事后诊断
        log.info("进程加固：未启用（默认）。%s",
                 hardening_report.get("reason", "如有注入类软件，诊断见 conflicts 命令。"))
    if migration.ran:
        if migration.ok:
            log.info("已从旧版目录迁移数据：%s", migration.summary())
        else:
            log.warning("旧版数据迁移不完整（不影响运行）：%s", migration.summary())
    return cfg, log, migration

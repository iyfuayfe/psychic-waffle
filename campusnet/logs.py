"""日志：只写文件，任何情况下都不弹窗、不写控制台（除非显式要求）。

pythonw.exe 启动时 sys.stderr 是 None，标准 logging 默认的 StreamHandler
会直接抛异常。所以这里手工装配 handler 并全程兜底。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_DIR, Config

LOGGER_NAME = "campusnet"
LOG_FILE = LOG_DIR / "campus_login.log"

_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")


def has_console() -> bool:
    for stream in (sys.stderr, sys.stdout):
        try:
            if stream is None or stream.closed or not stream.isatty():
                continue
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def setup_logging(cfg: Config | None = None, *, console: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    level_name = (cfg.log_level if cfg else "INFO") or "INFO"
    level = getattr(logging, str(level_name).upper(), logging.INFO)

    # 1) 文件日志（唯一的常态输出通道）
    wrote_file = False
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE,
            maxBytes=int(cfg.log_max_bytes) if cfg else 1_000_000,
            backupCount=int(cfg.log_backups) if cfg else 3,
            encoding="utf-8",
        )
        fh.setFormatter(_FMT)
        fh.setLevel(level)
        logger.addHandler(fh)
        wrote_file = True
    except Exception:  # noqa: BLE001
        wrote_file = False

    # 2) 控制台日志（仅 argparse 显式要求 + 真的有控制台时）
    if console and has_console():
        try:
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(_FMT)
            ch.setLevel(logging.DEBUG)
            logger.addHandler(ch)
        except Exception:  # noqa: BLE001
            pass

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    if not wrote_file:
        logger.warning("文件日志不可用，本次运行只写入内存")
    return logger

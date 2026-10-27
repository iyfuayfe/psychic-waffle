"""日志：只写文件，任何情况下都不弹窗、不写控制台（除非显式要求）。

V3.0 的两点变化：

1. 日志默认写到**用户数据目录**（``%LOCALAPPDATA%\\CampusNet\\logs``），
   不再写安装目录 —— 打包后的安装目录通常不可写。
2. 所有日志输出都经过 :class:`RedactingFormatter`，把密码、账号、令牌、
   长密文统一打码。这样即使某处代码不小心把凭据拼进了消息，落盘也是脱敏的。

pythonw.exe / windowed EXE 启动时 sys.stderr 是 None，标准 logging 默认的
StreamHandler 会直接抛异常，所以这里手工装配 handler 并全程兜底。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import paths
from .config import Config
from .security import redact_text

LOGGER_NAME = "campusnet"
LOG_DIR = paths.log_dir()
LOG_FILE = paths.log_file()

_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class RedactingFormatter(logging.Formatter):
    """格式化之后再脱敏一次，连 traceback 里的内容也覆盖到。"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        text = super().format(record)
        try:
            return redact_text(text)
        except Exception:  # noqa: BLE001  脱敏失败也不能丢日志
            return text


def has_console() -> bool:
    for stream in (sys.stderr, sys.stdout):
        try:
            if stream is None or stream.closed or not stream.isatty():
                continue
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def setup_logging(cfg: Config | None = None, *, console: bool = False,
                  log_dir_override=None) -> logging.Logger:
    """装配 logger。

    ``log_dir_override`` 主要给测试用（把日志写到临时目录）。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass

    level_name = (cfg.log_level if cfg else "INFO") or "INFO"
    level = getattr(logging, str(level_name).upper(), logging.INFO)

    formatter = RedactingFormatter(_FMT, _DATEFMT)
    directory = log_dir_override or (paths.log_dir() if cfg is None else paths.log_dir())

    # 1) 文件日志（唯一的常态输出通道）
    wrote_file = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "campus_login.log",
            maxBytes=int(cfg.log_max_bytes) if cfg else 1_000_000,
            backupCount=int(cfg.log_backups) if cfg else 3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.setLevel(level)
        logger.addHandler(handler)
        wrote_file = True
    except Exception:  # noqa: BLE001
        wrote_file = False

    # 2) 控制台日志（仅显式要求 + 真的有控制台时）
    if console and has_console():
        try:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(formatter)
            stream.setLevel(logging.DEBUG)
            logger.addHandler(stream)
        except Exception:  # noqa: BLE001
            pass

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    if not wrote_file:
        logger.warning("文件日志不可用，本次运行只写入内存")
    return logger


def get_logger() -> logging.Logger:
    """拿一个可用的 logger（没初始化过就兜底成内存日志）。"""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def log_file_path():
    """当前日志文件路径（GUI 的"打开日志"用）。"""
    return paths.log_file()

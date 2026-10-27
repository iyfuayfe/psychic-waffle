"""单一版本号来源。

所有入口、EXE 资源、构建脚本都从这里取版本，避免出现三处不一致。
发布前改这里 + docs/CHANGELOG.md 即可。
"""

from __future__ import annotations

APP_NAME = "CampusNet"
APP_NAME_CN = "校园网自动登录"
APP_SUBTITLE = "自动连接 · 网页认证"

# 语义化版本：MAJOR.MINOR.PATCH
__version__ = "3.1.2"
VERSION_TUPLE = tuple(int(x) for x in __version__.split(".")[:3])

# 配置文件结构版本（和程序版本解耦：只有配置结构变化时才 +1）
CONFIG_SCHEMA_VERSION = 3

# 用户数据目录名（%LOCALAPPDATA%\<DATA_DIR_NAME>）
DATA_DIR_NAME = "CampusNet"

# 计划任务名（用户级，删除时只动这一个）
TASK_NAME = "CampusNetAutoLogin"

# 单实例互斥体名（基础名；实际使用时会按数据目录加后缀，见 mutex_name()）
MUTEX_NAME = r"Local\CampusNetDaemonV3"

# 兼容旧版本使用过的资源名
LEGACY_MUTEX_NAMES = (r"Local\CampusNetAutoLogin",)


def mutex_name(data_dir_key: str = "") -> str:
    """守护进程单实例互斥体名。

    为什么按数据目录区分：命名互斥体是**全局对象**。如果只用一个固定名字，
    那么"属于另一个数据目录的旧守护"（换过数据目录、便携版与安装版并存、
    或异常退出留下的实例）会把运行锁一直占着，导致当前配置的守护永远起不来，
    界面上还会一直显示"运行中但无响应"。加上数据目录指纹后互不干扰。

    ``data_dir_key`` 为空时退化为基础名（供不需要区分的场景使用）。
    """
    if not data_dir_key:
        return MUTEX_NAME
    return f"{MUTEX_NAME}-{data_dir_key}"


def version_text() -> str:
    """例如 ``3.0.0``。"""
    return __version__


def banner() -> str:
    """``CampusNet 3.0.0`` —— 用于日志与 ``--version``。"""
    return f"{APP_NAME} {__version__}"

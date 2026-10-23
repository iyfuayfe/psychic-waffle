# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir，四个 EXE 共用一份分析结果）。

为什么是 onedir 而不是 onefile：

* onedir 生成的目录结构清楚，DLL / 资源 / python 运行时都在 ``_internal`` 里，
  出问题能直接看到是哪个文件缺失；onefile 每次启动都要把整个运行时解压到
  ``%TEMP%``，启动慢、容易被杀毒软件反复扫、临时目录清理不当还会残留。
* 本程序要调用 Windows 组件（netsh、DPAPI、PowerShell），onedir 更稳。
* 四个 EXE 共享同一份 Analysis，发布体积比分别打包小得多。

入口只有一个 ``entry.py``：运行期按 EXE 自己的文件名分派角色
（CampusNet / CampusNetSetup / CampusNetService / CampusNetCLI）。

构建：``build_exe.bat``（或 ``python tools/build_exe.py``）。
"""

import os
import sys
from pathlib import Path

# ---- 允许直接 python packaging/CampusNet.spec 时不依赖 PyInstaller 的 cwd ----
SPEC_DIR = Path(SPECPATH).resolve()          # noqa: F821  (PyInstaller 注入)
PROJECT_ROOT = SPEC_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from campusnet.version import APP_NAME, __version__  # noqa: E402

ICON_FILE = SPEC_DIR / "app.ico"
ICON = str(ICON_FILE) if ICON_FILE.exists() else None
VERSION_FILE = SPEC_DIR / "version_info.txt"
VERSION_RES = str(VERSION_FILE) if VERSION_FILE.exists() else None

# tkinter 的隐式依赖（PyInstaller 的 hook 一般能带上，这里显式兜一层）
HIDDEN_IMPORTS = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.messagebox",
    "campusnet.app.cli",
    "campusnet.app.gui",
    "campusnet.app.service",
    "campusnet.app.setup",
    "campusnet.ui.main_window",
    "campusnet.ui.gui_service",
    "campusnet.ui.gui_common",
    "campusnet.ui.page_auth",
    "campusnet.ui.page_overview",
    "campusnet.ui.page_settings",
    "campusnet.ui.ui_kit",
]

# 这些不进发布包：测试、构建脚本、探针脚本、文档
EXCLUDES = [
    "pytest",
    "unittest",
    "tests",
    "tools",
    "packaging",
]

# 随程序分发的只读资源（示例配置等）；个人数据一律不打进去
DATA_FILES = []
for name in ("config.example.json", "THIRD_PARTY_NOTICES.md"):
    candidate = PROJECT_ROOT / name
    if candidate.exists():
        DATA_FILES.append((str(candidate), "."))

a = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821


def _exe(name: str, *, console: bool = False):
    """四个 EXE 共用同一份依赖，只有名字和"是否需要控制台"不同。"""
    return EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        icon=ICON,
        version=VERSION_RES,
        console=console,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
    )


exe_gui = _exe("CampusNet", console=False)
exe_setup = _exe("CampusNetSetup", console=False)
exe_service = _exe("CampusNetService", console=False)
# CLI 需要控制台，方便开发者直接看输出
exe_cli = _exe("CampusNetCLI", console=True)

coll = COLLECT(  # noqa: F821
    exe_gui,
    exe_setup,
    exe_service,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

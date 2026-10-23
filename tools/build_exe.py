"""一键构建 Windows EXE（PyInstaller onedir），并做构建后自检。

用法::

    build_exe.bat                        # 推荐：双击即可
    python tools/build_exe.py            # 等价
    python tools/build_exe.py --clean    # 先清掉 build/dist 再构建
    python tools/build_exe.py --keep-log # 额外保留 PyInstaller 的详细日志

做了什么：

1. 检查 Python 版本与 tkinter（GUI 必需）；
2. 检查 PyInstaller 是否可用，不在就给出**明确的安装命令**而不是报错退出；
3. 从 ``campusnet/version.py`` 生成 Windows 版本信息资源
   （EXE 属性里能看到产品名 / 版本 / 版权）；
4. 调用 PyInstaller 生成 ``dist/CampusNet/``；
5. 自检：四个 EXE 是否都在、是否能在不依赖系统 Python 的情况下跑起来
   （``CampusNetCLI.exe version`` / ``paths``）；
6. 把构建结果写进 ``build/build-report.txt``，并在控制台打印摘要。

注意：**不要在打包后的机器上 pretend 验证**。这里只负责构建与本机冒烟，
真实 Windows 验收清单见 ``docs/WINDOWS_ACCEPTANCE.md``。
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC = PROJECT_ROOT / "packaging" / "CampusNet.spec"
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
LOG_PATH = BUILD_DIR / "build.log"
REPORT_PATH = BUILD_DIR / "build-report.txt"

EXE_NAMES = ("CampusNet.exe", "CampusNetSetup.exe", "CampusNetService.exe", "CampusNetCLI.exe")


def say(message: str = "") -> None:
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def fail(message: str) -> int:
    say("")
    say("[失败] " + message)
    return 1


def check_environment() -> str:
    """返回 PyInstaller 的调用方式（"pyinstaller" 或 "module"）。"""
    if sys.version_info < (3, 9):
        raise SystemExit(fail(f"Python 版本过低：{sys.version.split()[0]}，需要 3.9+"))

    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(fail(f"当前 Python 没有 tkinter，无法打包图形界面：{exc}"))

    if shutil.which("pyinstaller"):
        return "exe"
    try:
        import PyInstaller  # noqa: F401
    except Exception:
        say("[提示] 没有找到 PyInstaller。请先执行：")
        say("       python -m pip install -r requirements-dev.txt")
        raise SystemExit(fail("缺少构建依赖 PyInstaller"))
    return "module"


def find_pyinstaller() -> str:
    exe = shutil.which("pyinstaller")
    if exe:
        return exe
    return ""


def write_version_info() -> Path:
    """生成 Windows 版本信息资源（EXE 属性页显示的东西）。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    from campusnet.version import APP_NAME, APP_NAME_CN, __version__  # noqa: PLC0415

    parts = [int(x) for x in __version__.split(".")][:4]
    while len(parts) < 4:
        parts.append(0)
    version_tuple = tuple(parts)
    version_text = ".".join(str(p) for p in version_tuple)

    content = f"""# UTF-8
# 由 tools/build_exe.py 自动生成，请不要手工修改
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'CampusNet'),
         StringStruct('FileDescription', '{APP_NAME_CN}（{APP_NAME} {__version__}）'),
         StringStruct('FileVersion', '{version_text}'),
         StringStruct('InternalName', '{APP_NAME}'),
         StringStruct('LegalCopyright', 'MIT License'),
         StringStruct('OriginalFilename', 'CampusNet.exe'),
         StringStruct('ProductName', '{APP_NAME_CN}'),
         StringStruct('ProductVersion', '{version_text}')])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])])
  ]
)
"""
    target = PROJECT_ROOT / "packaging" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    say(f"[1/5] 已生成版本资源：{target.relative_to(PROJECT_ROOT)}（{version_text}）")
    return target


def run_pyinstaller(mode: str, clean: bool, keep_log: bool) -> int:
    if clean:
        for path in (BUILD_DIR, DIST_DIR):
            if path.exists():
                say(f"[清理] 删除 {path.relative_to(PROJECT_ROOT)}")
                shutil.rmtree(path, ignore_errors=True)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    args = (
        [str(Path(sys.executable)), "-m", "PyInstaller"]
        if mode == "module"
        else ["pyinstaller"]
    )
    command = [
        *args,
        str(SPEC),
        "--noconfirm",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "work"),
    ]
    if clean:
        command.append("--clean")

    say("[2/5] 正在构建（首次构建通常需要 1-3 分钟）…")
    say("      " + subprocess.list2cmdline(command))
    started = time.time()
    with open(LOG_PATH, "w", encoding="utf-8", errors="replace") as log:
        log.write("命令：" + subprocess.list2cmdline(command) + "\n")
        log.write("时间：" + datetime.datetime.now().isoformat() + "\n")
        log.flush()
        proc = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log.write(proc.stdout or "")
        log.flush()
    elapsed = time.time() - started

    if proc.returncode != 0:
        say((proc.stdout or "")[-4000:])
        return fail(f"PyInstaller 构建失败（退出码 {proc.returncode}），完整日志：{LOG_PATH}")
    say(f"[3/5] 构建完成，用时 {elapsed:.1f}s，日志：{LOG_PATH.relative_to(PROJECT_ROOT)}")
    if not keep_log:
        pass
    return 0


def smoke_test(dist: Path) -> tuple[bool, list]:
    """在**不依赖系统 Python** 的前提下跑一下 CLI，确认 EXE 真的能用。"""
    results = []
    cli = dist / "CampusNetCLI.exe"
    if not cli.exists():
        return False, [(f"{cli.name} 不存在", False, "")]

    env = dict(os.environ)
    # 关键：把自己的解释器路径从 PATH 里拿掉，避免"其实是系统 Python 在跑"
    env["PATH"] = r"C:\Windows\System32;C:\Windows"
    env["PYTHONHOME"] = ""
    env["PYTHONPATH"] = ""
    for args, label in (
        (["version"], "CampusNetCLI version"),
        (["paths"], "CampusNetCLI paths"),
    ):
        try:
            proc = subprocess.run(
                [str(cli), *args],
                capture_output=True,
                timeout=90,
                env=env,
                cwd=str(dist),
            )
            out = (proc.stdout or b"").decode("utf-8", "replace") + (
                proc.stderr or b""
            ).decode("utf-8", "replace")
            ok = proc.returncode == 0 and bool(out.strip())
            results.append((label, ok, out.strip()[:500]))
        except Exception as exc:  # noqa: BLE001
            results.append((label, False, str(exc)))
    return all(item[1] for item in results), results


def copy_release_extras(dist: Path) -> None:
    """把用户/合规需要的文件放到发布目录**顶层**（PyInstaller 的 datas 会落到 _internal）。

    放在顶层是为了让用户一眼看到 `THIRD_PARTY_NOTICES.md`、示例配置，以及
    面向普通用户的启动 / 卸载脚本（`0-` 到 `4-`），使发布包开箱即用。
    """
    extras = ("THIRD_PARTY_NOTICES.md", "LICENSE", "config.example.json")
    for name in extras:
        source = PROJECT_ROOT / name
        if not source.exists():
            continue
        try:
            shutil.copy2(source, dist / name)
        except Exception as exc:  # noqa: BLE001
            say(f"      [警告] 复制 {name} 失败：{exc}")
    for source in sorted(PROJECT_ROOT.glob("[0-9]-*.bat")):
        try:
            shutil.copy2(source, dist / source.name)
        except Exception as exc:  # noqa: BLE001
            say(f"      [警告] 复制 {source.name} 失败：{exc}")


def write_report(dist: Path, smoke: list, elapsed_note: str) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from campusnet.version import __version__  # noqa: PLC0415

    lines = [
        "CampusNet EXE 构建报告",
        "=" * 60,
        f"时间       : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"版本       : {__version__}",
        f"Python     : {sys.version.split()[0]} ({sys.executable})",
        f"平台       : {sys.platform}",
        f"输出目录   : {dist}",
        f"说明       : {elapsed_note}",
        "",
        "生成的文件：",
    ]
    total = 0
    if dist.exists():
        for item in sorted(dist.rglob("*")):
            if item.is_file():
                total += item.stat().st_size
        for name in EXE_NAMES:
            exe = dist / name
            if exe.exists():
                lines.append(f"  {name:22} {exe.stat().st_size / 1024:8.0f} KB")
            else:
                lines.append(f"  {name:22} 缺失！")
        lines.append(f"  合计体积（含 _internal）: {total / 1024 / 1024:.1f} MB")
    lines += ["", "自检结果："]
    for label, ok, detail in smoke:
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if detail and not ok:
            lines.append(f"         {detail}")
    lines += [
        "",
        "提示：本报告只覆盖构建与 CLI 冒烟。GUI/向导/服务的真实交互请在 Windows 上按",
        "      docs/WINDOWS_ACCEPTANCE.md 逐项验收。",
    ]
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"[5/5] 构建报告：{REPORT_PATH.relative_to(PROJECT_ROOT)}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 CampusNet EXE")
    parser.add_argument("--clean", action="store_true", help="构建前清掉 build/ 与 dist/")
    parser.add_argument("--keep-log", action="store_true", help="保留 PyInstaller 详细日志")
    parser.add_argument("--no-smoke", action="store_true", help="跳过构建后自检")
    args = parser.parse_args(argv)

    say("=" * 60)
    say("CampusNet EXE 构建")
    say("=" * 60)

    mode = check_environment()
    write_version_info()

    code = run_pyinstaller(mode, args.clean, args.keep_log)
    if code != 0:
        return code

    dist = DIST_DIR / "CampusNet"
    if not dist.exists():
        return fail(f"没有生成预期的输出目录：{dist}")

    say("[4/5] 检查生成的文件 …")
    missing = [name for name in EXE_NAMES if not (dist / name).exists()]
    if missing:
        return fail("缺少 EXE：" + "、".join(missing))
    say("      四个 EXE 都在：" + "、".join(EXE_NAMES))
    copy_release_extras(dist)

    smoke: list = []
    if not args.no_smoke:
        ok, smoke = smoke_test(dist)
        for label, passed, detail in smoke:
            say(f"      [{'PASS' if passed else 'FAIL'}] {label}")
            if not passed and detail:
                say("             " + detail)
    else:
        smoke = [("跳过自检（--no-smoke）", True, "")]

    write_report(dist, smoke, "PyInstaller onedir 构建" + ("（--clean）" if args.clean else ""))
    say("")
    say("构建成功：")
    say(f"  发布目录 : {dist}")
    for name in EXE_NAMES:
        say(f"  - {name}")
    say("")
    say("注意：请在一台**没有安装 Python** 的 Windows 上验证以下命令，")
    say("      才能确认发布包不依赖系统 Python：")
    say(f'      "{dist / "CampusNetCLI.exe"}" version')
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""统一的测试入口：跑单元测试 + 旧版遗留的脚本测试，输出一张汇总表。

用法::

    python tools/run_tests.py             # 全部
    python tools/run_tests.py --unit      # 只跑 tests/ 下的 unittest
    python tools/run_tests.py --legacy    # 只跑 tools/test_*.py
    python tools/run_tests.py --exe       # 额外跑 EXE 冒烟（需要先构建）

为什么保留两套：

* ``tests/`` 是 V3.0 新增的 unittest 套件，覆盖配置、路径、安全、状态机；
* ``tools/test_*.py`` 是 V2 留下的脚本式自测（AES 向量、netsh 输出解析、
  界面冒烟），它们验证的是别的东西，有价值的就继续跑，不重写。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

UNIT_SUITES = [
    "tests.test_security",
    "tests.test_config",
    "tests.test_paths",
    "tests.test_checker",
    "tests.test_http",
    "tests.test_authenticator",
    "tests.test_daemon",
    "tests.test_autostart",
    "tests.test_uninstall",
    "tests.test_conflicts",
    "tests.test_hardening",
    "tests.test_status",
    "tests.test_probe_portal",
    "tests.test_gui_service_logger",
    "tests.test_wifi_detection",
    "tests.test_ui_responsiveness",
    "tests.test_portal_js",
    "tests.test_release_hygiene",
]

LEGACY_SCRIPTS = [
    ("AES 已知向量（tools/test_aes.py）", ["tools/test_aes.py"]),
    ("netsh 输出解析（tools/test_wifi.py）", ["tools/test_wifi.py"]),
    ("界面冒烟（tools/test_gui_smoke.py）", ["tools/test_gui_smoke.py"]),
    ("浏览器自动填写（tools/test_web_autofill.py）", ["tools/test_web_autofill.py"]),
]

# 需要构建产物 / 真实系统权限的集成测试（默认不跑，避免误改系统）
INTEGRATION_SCRIPTS = [
    ("本机端到端认证（tools/test_local_e2e.py）", ["tools/test_local_e2e.py"]),
    ("系统集成（tools/test_system_integration.py）", ["tools/test_system_integration.py"]),
]


def cleanup_leftovers() -> list:
    """跑每个脚本套件前清掉上一套件可能留下的后台进程与测试数据。

    为什么需要：EXE 冒烟会拉起 `CampusNetService.exe`，系统集成也会起后台守护。
    如果前一个套件的守护没退干净，它会占着**运行锁**，后一个套件的
    `start` 就会失败（实测：单独跑全绿、连起来跑失败）。这属于测试环境清理，
    不是产品逻辑问题，但必须在 runner 里做掉，否则结果会时好时坏。
    """
    done: list = []
    targets = ("CampusNetService.exe", "CampusNetCLI.exe", "CampusNet.exe")
    for name in targets:
        try:
            proc = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                done.append(f"结束遗留进程 {name}")
        except Exception:  # noqa: BLE001
            continue
    smoke = PROJECT_ROOT / "build" / "smoke-data"
    if smoke.exists():
        try:
            shutil.rmtree(smoke, ignore_errors=True)
            done.append("清理 build/smoke-data")
        except Exception:  # noqa: BLE001
            pass
    # 单元测试里起的守护是 `python -m campusnet.app.service --daemon`，
    # 不在上面的进程名里；漏掉它会占着运行锁，害得后面的套件 start 失败。
    try:
        listing = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -match 'campusnet.app.service' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for token in (listing.stdout or b"").decode("utf-8", "replace").split():
            if token.strip().isdigit():
                subprocess.run(
                    ["taskkill", "/F", "/PID", token.strip()],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                done.append(f"结束遗留 Python 守护进程 {token.strip()}")
    except Exception:  # noqa: BLE001
        pass
    if done:
        time.sleep(1.2)
    return done


def run(command: list, *, label: str) -> tuple[bool, str, float]:
    started = time.time()
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for note in cleanup_leftovers():
        print(f"  · {note}")
    proc = subprocess.run(
        [sys.executable, *command],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        timeout=900,
        env=env,
    )
    elapsed = time.time() - started
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    text = (out + ("\n" + err if err.strip() else "")).strip()
    return proc.returncode == 0, text, elapsed


def tail(text: str, lines: int = 3) -> str:
    parts = [line for line in text.splitlines() if line.strip()]
    return " | ".join(parts[-lines:])[:300] if parts else ""


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 CampusNet 的全部测试")
    parser.add_argument("--unit", action="store_true", help="只跑单元测试")
    parser.add_argument("--legacy", action="store_true", help="只跑旧版脚本测试")
    parser.add_argument("--exe", action="store_true", help="额外跑 EXE 冒烟测试")
    parser.add_argument("--integration", action="store_true",
                        help="额外跑端到端认证与系统集成（会临时改 HKCU Run，结束还原）")
    parser.add_argument("--all", action="store_true", help="等价于 --exe --integration")
    args = parser.parse_args(argv)

    if args.all:
        args.exe = True
        args.integration = True

    run_unit = args.unit or not (args.unit or args.legacy)
    run_legacy = args.legacy or not (args.unit or args.legacy)

    results: list = []

    if run_unit:
        print("=" * 70)
        print("单元测试（unittest）")
        print("=" * 70)
        for suite in UNIT_SUITES:
            ok, text, elapsed = run(["-m", "unittest", suite], label=suite)
            results.append((suite, ok, elapsed, tail(text)))
            print(f"  [{'PASS' if ok else 'FAIL'}] {suite}  ({elapsed:.1f}s)")
            if not ok:
                print("      " + tail(text, 8))

    if run_legacy:
        print()
        print("=" * 70)
        print("旧版脚本测试")
        print("=" * 70)
        for label, command in LEGACY_SCRIPTS:
            script = PROJECT_ROOT / command[0]
            if not script.exists():
                results.append((label, False, 0.0, "脚本不存在"))
                print(f"  [SKIP] {label}（文件不存在）")
                continue
            ok, text, elapsed = run(command, label=label)
            results.append((label, ok, elapsed, tail(text)))
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}  ({elapsed:.1f}s)")
            if not ok:
                print("      " + tail(text, 8))

    if args.exe:
        print()
        print("=" * 70)
        print("EXE 冒烟测试")
        print("=" * 70)
        ok, text, elapsed = run(["tools/test_exe_smoke.py"], label="exe")
        results.append(("EXE 冒烟", ok, elapsed, tail(text)))
        print(f"  [{'PASS' if ok else 'FAIL'}] EXE 冒烟  ({elapsed:.1f}s)")
        if not ok:
            print("      " + tail(text, 8))

    if args.integration:
        print()
        print("=" * 70)
        print("集成测试（本机模拟门户；系统集成会临时修改 HKCU Run 并在结束时还原）")
        print("=" * 70)
        for label, command in INTEGRATION_SCRIPTS:
            script = PROJECT_ROOT / command[0]
            if not script.exists():
                results.append((label, False, 0.0, "脚本不存在"))
                print(f"  [SKIP] {label}（文件不存在）")
                continue
            ok, text, elapsed = run(command, label=label)
            results.append((label, ok, elapsed, tail(text)))
            print(f"  [{'PASS' if ok else 'FAIL'}] {label}  ({elapsed:.1f}s)")
            if not ok:
                print("      " + tail(text, 10))

    print()
    print("=" * 70)
    passed = sum(1 for _n, ok, _e, _d in results if ok)
    print(f"汇总：{passed}/{len(results)} 组通过")
    for name, ok, elapsed, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:34} {elapsed:6.1f}s")
        if not ok and detail:
            print(f"        {detail}")
    print("=" * 70)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

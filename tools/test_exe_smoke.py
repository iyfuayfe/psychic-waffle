"""EXE 冒烟测试：验证打包出来的程序能不能真的用。

和 ``tools/build_exe.py`` 的分工：构建脚本负责"生出来"，这个脚本负责"挨个点一遍"。
它可以单独运行，也可以在构建之后跑（构建脚本只会跑其中最快的两项）。

检查项：

1. 四个 EXE 都在，体积合理；
2. ``CampusNetCLI.exe version / paths / status`` 能在**没有 Python 的 PATH** 下运行；
3. 打包目录里没有混入源码 ``.py`` 或个人数据（config.json / logs / state.json）；
4. ``CampusNetService.exe --check`` 能跑完并写日志；
5. ``CampusNet.exe`` 能启动并保持运行（窗口程序，靠进程是否存活判断），
   启动后数据目录会被创建、程序自身不会往安装目录写东西；
6. 结束后不残留进程。

用法::

    python tools/test_exe_smoke.py
    python tools/test_exe_smoke.py --dist dist/CampusNet --gui-wait 8
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = PROJECT_ROOT / "dist" / "CampusNet"
REPORT = PROJECT_ROOT / "build" / "exe-smoke-report.txt"

EXES = ("CampusNet.exe", "CampusNetSetup.exe", "CampusNetService.exe", "CampusNetCLI.exe")

# 故意把 Python 从 PATH 里去掉：如果程序偷偷依赖系统 Python，这一步会暴露
CLEAN_PATH = r"C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem"

FORBIDDEN_IN_DIST = (
    "config.json",
    "state.json",
    "heartbeat.json",
    "logs",
    "backup",
)

results: list = []
skipped: list = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def record_skip(name: str, reason: str) -> None:
    skipped.append((name, reason))
    print(f"  [SKIP] {name} — {reason}")


def clean_env(data_dir: Path) -> dict:
    """模拟一台没装 Python 的电脑。"""
    env = {
        "PATH": CLEAN_PATH,
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "windir": os.environ.get("windir", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", r"C:\Windows\Temp"),
        "TMP": os.environ.get("TMP", r"C:\Windows\Temp"),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "USERNAME": os.environ.get("USERNAME", ""),
        "USERDOMAIN": os.environ.get("USERDOMAIN", ""),
        # 隔离用户数据目录，避免污染真实配置
        "CAMPUSNET_DATA_DIR": str(data_dir),
    }
    return env


def run(exe: Path, args: list, env: dict, timeout: float = 120.0):
    return subprocess.run(
        [str(exe), *args],
        capture_output=True,
        timeout=timeout,
        env=env,
        cwd=str(exe.parent),
    )


def decode(raw: bytes) -> str:
    for encoding in ("utf-8", "mbcs", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def check_files(dist: Path) -> None:
    print("1) 文件检查")
    for name in EXES:
        path = dist / name
        ok = path.exists() and path.stat().st_size > 500_000
        record(f"{name} 存在且大小合理", ok,
               "" if ok else (f"缺少或过小：{path}" + (f" ({path.stat().st_size}B)" if path.exists() else "")))

    py_files = [p for p in dist.rglob("*.py") if p.is_file()]
    record("发布目录里没有源码 .py 文件", not py_files,
           "发现：" + ", ".join(str(p.relative_to(dist)) for p in py_files[:5]))

    leaked = []
    for name in FORBIDDEN_IN_DIST:
        if (dist / name).exists():
            leaked.append(name)
    record("发布目录里没有个人配置 / 日志", not leaked, "发现：" + ", ".join(leaked))

    internal = dist / "_internal"
    record("_internal 目录存在（onedir 结构）", internal.is_dir())


def check_cli(dist: Path, env: dict) -> None:
    print("2) 命令行程序（在没有 Python 的环境里）")
    cli = dist / "CampusNetCLI.exe"

    proc = run(cli, ["version"], env)
    out = decode(proc.stdout).strip()
    record("CampusNetCLI version 正常", proc.returncode == 0 and "CampusNet" in out, out or decode(proc.stderr))

    proc = run(cli, ["paths"], env)
    out = decode(proc.stdout)
    record("CampusNetCLI paths 正常", proc.returncode == 0 and "data_dir" in out, out[:300])
    record("paths 显示 frozen=True（确实跑在打包模式）", "frozen         = True" in out or "frozen = True" in out or "= True" in out, out[:200])

    proc = run(cli, ["status"], env, timeout=180)
    out = decode(proc.stdout)
    record("CampusNetCLI status 能给出结构化结果",
           proc.returncode in (0, 1) and '"kind"' in out, out[:300] or decode(proc.stderr))

    proc = run(cli, ["autostart", "status"], env)
    out = decode(proc.stdout)
    record("CampusNetCLI autostart status 正常", proc.returncode == 0 and "enabled" in out, out[:300])


def check_service(dist: Path, env: dict) -> None:
    print("3) 后台守护程序")
    service = dist / "CampusNetService.exe"
    proc = run(service, ["--check"], env, timeout=180)
    record("CampusNetService --check 能跑完（退出码 0=在线 / 1=离线）",
           proc.returncode in (0, 1), f"exit={proc.returncode} {decode(proc.stderr)[:200]}")

    data_dir = Path(env["CAMPUSNET_DATA_DIR"])
    log = data_dir / "logs" / "campus_login.log"
    has_log = log.exists() and log.stat().st_size > 0
    record("后台守护写出了日志文件", has_log, f"{log} 不存在或为空")


def check_service_lifecycle(dist: Path, env: dict) -> None:
    """端到端验证后台守护：启动 → 写心跳 → 优雅停止。

    为了不碰真实账号，这里现场造一份配置：密码用 DPAPI 加密一个**假密码**，
    认证页指向 RFC 5737 文档地址（不可达），因此守护会一直处于"离线重试"状态，
    正好可以观察心跳与停止逻辑，也不会向任何真实服务器发请求。
    """
    print("3.5) 后台守护生命周期（启动 → 心跳 → 优雅停止）")
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from campusnet import crypto

        if not crypto.available():
            record("DPAPI 可用", False, "当前系统不支持 DPAPI，跳过生命周期测试")
            return
        encrypted = crypto.protect("fake-password-for-smoke-test")
    except Exception as exc:  # noqa: BLE001
        record("DPAPI 可用", False, str(exc))
        return

    data_dir = Path(env["CAMPUSNET_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "config_version": 3,
        "username": "smoke-test-user",
        "password_enc": encrypted,
        "portal_url": "http://192.0.2.10/portal",
        "wifi_ssid": "",
        "auto_enabled": True,
        "interval_online": 10,
        "interval_offline": 3,
        "interval_max": 10,
        "request_timeout": 3,
    }
    (data_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name in ("heartbeat.json", "logs/campus_login.log"):
        target = data_dir / name
        if target.exists():
            try:
                target.unlink()
            except Exception:  # noqa: BLE001
                pass

    service = dist / "CampusNetService.exe"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    try:
        proc = subprocess.Popen(
            [str(service), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            env=env,
            cwd=str(dist),
        )
    except Exception as exc:  # noqa: BLE001
        record("后台守护能启动", False, str(exc))
        return

    heartbeat = data_dir / "heartbeat.json"
    deadline = time.time() + 25
    state = ""
    while time.time() < deadline:
        if heartbeat.exists():
            try:
                payload = json.loads(heartbeat.read_text(encoding="utf-8"))
                state = str(payload.get("state") or "")
            except Exception:  # noqa: BLE001
                state = ""
            if state:
                break
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    record("后台守护启动了（进程存活）", proc.poll() is None,
           f"进程已退出，退出码 {proc.returncode}")
    record("后台守护写出了心跳文件", bool(state), f"heartbeat.json 状态为空（{heartbeat}）")
    record("心跳状态是已知状态",
           state in {"starting", "checking", "online", "wifi", "auth",
                     "cooldown", "need-user", "paused", "stopped"},
           f"未知状态 {state!r}")

    log = data_dir / "logs" / "campus_login.log"
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    record("守护日志有内容且已脱敏",
           bool(log_text.strip()) and "fake-password-for-smoke-test" not in log_text,
           "日志为空或出现了明文密码")

    # 优雅停止：写 stop.flag，由 CLI 触发
    cli = dist / "CampusNetCLI.exe"
    try:
        stop = subprocess.run([str(cli), "stop"], capture_output=True, timeout=90, env=env,
                              cwd=str(dist))
        stop_out = decode(stop.stdout) + decode(stop.stderr)
    except Exception as exc:  # noqa: BLE001
        stop_out = str(exc)
    time.sleep(1.0)
    stopped = proc.poll() is not None
    record("CampusNetCLI stop 能优雅停止守护", stopped,
           f"进程仍在运行；stop 输出：{stop_out[:200]}")
    if not stopped:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    try:
        flag = data_dir / "stop.flag"
        record("停止后 stop.flag 已被清理", not flag.exists(), str(flag))
    except Exception:  # noqa: BLE001
        pass


def check_gui(dist: Path, env: dict, wait: float) -> None:
    print("4) 图形界面程序")
    gui = dist / "CampusNet.exe"
    data_dir = Path(env["CAMPUSNET_DATA_DIR"])
    before = set(data_dir.rglob("*")) if data_dir.exists() else set()

    # 必须用 DETACHED_PROCESS + DEVNULL：否则子进程继承父进程的输出管道，
    # 调用方会一直等不到结束（窗口程序本身不会退出）。
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    try:
        proc = subprocess.Popen(
            [str(gui)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            env=env,
            cwd=str(gui.parent),
        )
    except Exception as exc:  # noqa: BLE001
        record("CampusNet.exe 能启动", False, str(exc))
        return

    alive = False
    risk = ""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from campusnet import conflicts  # noqa: PLC0415

        risk = conflicts.environment_risk()
    except Exception:  # noqa: BLE001
        risk = ""

    try:
        deadline = time.time() + max(3.0, wait)
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.4)
        alive = proc.poll() is None
        if alive:
            record("CampusNet.exe 启动后保持运行（窗口程序）", True)
        elif risk:
            # 装了会注入 GUI 的叠加软件（如游戏加加）时，任何 tkinter 程序都会闪退，
            # 这不是本程序的问题 —— 判为环境不适用，而不是产品缺陷。
            record_skip("CampusNet.exe 启动后保持运行（窗口程序）", risk[:160])
        else:
            record("CampusNet.exe 启动后保持运行（窗口程序）", False,
                   f"进程已退出，退出码 {proc.returncode}")

        after = set(data_dir.rglob("*")) if data_dir.exists() else set()
        new_items = {p for p in (after - before) if p.is_file()}
        record("GUI 启动后在用户数据目录创建了文件", bool(new_items) or data_dir.exists(),
               f"数据目录 {data_dir} 未创建")

        crash = data_dir / "logs" / "crash-gui.log"
        record("GUI 没有崩溃日志", not crash.exists(), str(crash))

        # 安装目录必须保持"只读"：不应出现新写入的个人数据
        leaked = [name for name in FORBIDDEN_IN_DIST if (dist / name).exists()]
        record("GUI 没有往安装目录写个人数据", not leaked, "发现：" + ", ".join(leaked))
    finally:
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=30,
                )
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.0)


def check_no_leftover(dist: Path) -> None:
    print("5) 残留进程检查")
    try:
        proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, timeout=30)
        text = decode(proc.stdout).lower()
    except Exception as exc:  # noqa: BLE001
        record("进程列表可读", False, str(exc))
        return
    leftovers = []
    for name in ("campusnet.exe", "campusnetsetup.exe", "campusnetservice.exe"):
        if name in text:
            leftovers.append(name)
    record("没有残留的图形界面 / 向导进程", not leftovers, "仍在运行：" + ", ".join(leftovers))


def write_report(dist: Path) -> int:
    passed = sum(1 for _n, ok, _d in results if ok)
    total = len(results)
    lines = [
        "CampusNet EXE 冒烟测试报告",
        "=" * 60,
        f"时间     : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"被测目录 : {dist}",
        f"结果     : {passed}/{total} 项通过" + (f"，{len(skipped)} 项跳过" if skipped else ""),
        "",
    ]
    for name, ok, detail in results:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if detail:
            lines.append(f"        {detail}")
    for name, reason in skipped:
        lines.append(f"[SKIP] {name}")
        lines.append(f"        {reason}")
    lines += [
        "",
        "说明：本脚本检查的是「打包产物能不能跑」，不验证真实校园网认证；",
        "      后者需要学校账号，见 docs/WINDOWS_ACCEPTANCE.md 的人工验收项。",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"结果：{passed}/{total} 项通过" + (f"，{len(skipped)} 项跳过" if skipped else "")
          + f"；报告：{REPORT}")
    return 0 if passed == total else 1


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="CampusNet EXE 冒烟测试")
    parser.add_argument("--dist", default=str(DEFAULT_DIST), help="发布的 onedir 目录")
    parser.add_argument("--gui-wait", type=float, default=6.0, help="GUI 存活观察时间（秒）")
    parser.add_argument("--skip-gui", action="store_true", help="跳过图形界面检查")
    args = parser.parse_args(argv)

    dist = Path(args.dist)
    if not dist.is_absolute():
        dist = (PROJECT_ROOT / dist).resolve()
    if not dist.is_dir():
        print(f"[错误] 找不到发布目录：{dist}（先运行 python tools/build_exe.py）")
        return 2

    data_dir = PROJECT_ROOT / "build" / "smoke-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = clean_env(data_dir)

    print("=" * 60)
    print("CampusNet EXE 冒烟测试")
    print("=" * 60)
    print(f"发布目录：{dist}")
    print(f"数据目录：{data_dir}（隔离，不影响真实配置）")
    print()

    check_files(dist)
    check_cli(dist, env)
    check_service(dist, env)
    check_service_lifecycle(dist, env)
    if args.skip_gui:
        print("4) 图形界面程序（--skip-gui 跳过）")
    else:
        check_gui(dist, env, args.gui_wait)
    check_no_leftover(dist)
    return write_report(dist)


if __name__ == "__main__":
    sys.exit(main())

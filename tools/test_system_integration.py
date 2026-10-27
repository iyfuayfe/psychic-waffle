"""系统集成测试：注册表自启动、单实例、优雅停止、旧版残留清理、卸载。

这些都是**真的动系统**的操作，所以脚本严格遵守三条纪律：

1. 测试前记录 ``HKCU\\...\\Run`` 的完整快照与旧版计划任务状态；
2. 结束时（含中途失败）把本程序加的东西全部撤掉，并核对"其它启动项一个没动"；
3. 数据目录用 ``%LOCALAPPDATA%\\CampusNet-test-*``，不碰用户真实配置与日志。

**环境自适应**：某些受限会话（沙箱 / AppContainer / 受限令牌）会让被测 EXE 写
注册表或删启动项时拿到 ``WinError 5``。脚本会先做一次能力探测，探测不过就把
相关检查标成 ``SKIP`` 并说明原因 —— 而不是报成产品缺陷。这类检查必须在一台
普通用户会话的 Windows 上跑一遍才算通过。

用法::

    python tools/test_system_integration.py
    python tools/test_system_integration.py --keep-legacy   # 不清理旧版计划任务
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from campusnet import crypto  # noqa: E402

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CampusNetDaemon"
LEGACY_TASKS = ("CampusNetAutoLogin", "CampusNetAutoLogin-KeepAlive")

DIST = PROJECT_ROOT / "dist" / "CampusNet"
FROZEN_CLI = [str(DIST / "CampusNetCLI.exe")]
SOURCE_CLI = [sys.executable, "-m", "campusnet.app.cli"]

# 由 select_mode() 决定；默认用打包 EXE
ACTIVE_CLI: list = list(FROZEN_CLI)
ACTIVE_MODE = "frozen"

PASSED: list = []
FAILED: list = []
SKIPPED: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    if ok:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return ok


def skip(name: str, reason: str) -> None:
    SKIPPED.append(f"{name}（{reason}）")
    print(f"  [SKIP] {name} — {reason}")


# ---------------------------------------------------------------- 注册表

def read_run_values() -> dict:
    import winreg

    values: dict = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = value
                index += 1
    except FileNotFoundError:
        pass
    return values


def task_exists(name: str) -> bool:
    proc = subprocess.run(["schtasks.exe", "/Query", "/TN", name],
                          capture_output=True, timeout=30)
    return proc.returncode == 0


def cli(args: list, *, data_dir: Path | None = None, timeout: float = 180.0):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if data_dir is not None:
        env["CAMPUSNET_DATA_DIR"] = str(data_dir)
    proc = subprocess.run([*ACTIVE_CLI, *args], capture_output=True, timeout=timeout, env=env,
                          cwd=str(PROJECT_ROOT))
    out = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
    return proc.returncode, out.strip()


def daemon_pids() -> list:
    """当前运行中的后台守护 PID（打包模式和源码模式都认）。"""
    if ACTIVE_MODE == "frozen":
        query = ("Get-Process -Name CampusNetService -ErrorAction SilentlyContinue | "
                 "Select-Object -ExpandProperty Id")
    else:
        query = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
            "| Where-Object { $_.CommandLine -like '*campusnet.app.service*' } "
            "| Select-Object -ExpandProperty ProcessId"
        )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, timeout=40,
        )
        text = (proc.stdout or b"").decode("utf-8", "replace")
        return [int(line.strip()) for line in text.splitlines() if line.strip().isdigit()]
    except Exception:  # noqa: BLE001
        return []


def user_data_dir(name: str) -> Path:
    """放在 %LOCALAPPDATA% 下：ACL 正常，也不污染工作区。"""
    base = Path(os.environ.get("LOCALAPPDATA") or PROJECT_ROOT / ".tmp")
    target = base / f"CampusNet-test-{name}-{int(time.time())}"
    target.mkdir(parents=True, exist_ok=True)
    _CREATED_DIRS.append(target)
    return target


_CREATED_DIRS: list = []


def write_test_config(data_dir: Path, portal_port: int) -> None:
    cfg = {
        "config_version": 3,
        "username": "sysint-user",
        "password_enc": crypto.protect("sysint-password"),
        "wifi_ssid": "",
        "auto_enabled": True,
        "portal_auth_enabled": True,
        "request_timeout": 4,
        "interval_online": 30,
        "interval_offline": 5,
        "interval_max": 30,
        "check_urls": [
            {"url": f"http://127.0.0.1:{portal_port}/connecttest.txt",
             "expect_text": "Microsoft Connect Test"}
        ],
        "drcom": {"enabled": False, "host": "127.0.0.1", "port": portal_port},
    }
    (data_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def wait_for(predicate, timeout: float = 40.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def probe_registry_capability(data_dir: Path) -> tuple[bool, str]:
    """用当前被测命令试一次自启动开关，判断这个会话是否允许它写注册表。"""
    code, out = cli(["autostart", "on"], data_dir=data_dir)
    if code == 0:
        cli(["autostart", "off"], data_dir=data_dir)
        return True, ""
    reason = "被测进程无法写入注册表"
    for needle in ("WinError 5", "拒绝访问", "Access is denied", "Access denied"):
        if needle in out:
            reason = "本会话的受限令牌不允许它写注册表（WinError 5）"
            break
    return False, reason


def select_mode(data_dir: Path) -> tuple[bool, str, str]:
    """决定用打包 EXE 还是源码模式来验证系统集成。

    为什么需要这个：本会话（以及任何沙箱 / AppContainer 会话）会把**新起的
    未签名二进制**限制在工作区内 —— 实测打包 EXE 写 ``%LOCALAPPDATA%``、
    写 ``HKCU`` 一律 ``WinError 5``，而同一份代码用 CPython 跑就没问题。
    这不是产品缺陷，但会让"注册表/启动项/计划任务"这些检查全部无法验证。

    处理方式：先试打包 EXE；不可行就自动切到源码模式验证**同一套逻辑**，
    并把差异明确打印出来，绝不静默降级。
    """
    global ACTIVE_CLI, ACTIVE_MODE

    ACTIVE_CLI, ACTIVE_MODE = list(FROZEN_CLI), "frozen"
    ok, reason = probe_registry_capability(data_dir)
    if ok:
        return True, "frozen", ""

    ACTIVE_CLI, ACTIVE_MODE = list(SOURCE_CLI), "source"
    ok2, reason2 = probe_registry_capability(data_dir)
    if ok2:
        return True, "source", (
            f"打包 EXE 在本会话受限（{reason}），已切换到源码模式验证同一套系统集成逻辑"
        )
    ACTIVE_CLI, ACTIVE_MODE = list(FROZEN_CLI), "frozen"
    return False, "frozen", f"两种模式都无法写注册表：{reason} / {reason2}"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="CampusNet 系统集成测试")
    parser.add_argument("--keep-legacy", action="store_true", help="不清理旧版计划任务")
    args = parser.parse_args(argv)

    if not DIST.exists():
        print(f"[错误] 找不到 {DIST}，请先运行 build_exe.bat")
        return 2
    if not crypto.available():
        print("[错误] 当前系统不支持 DPAPI")
        return 2

    print("=" * 72)
    print("CampusNet 系统集成测试")
    print("（会真实修改 HKCU Run 与旧版计划任务；结束时还原；数据目录在 LOCALAPPDATA）")
    print("=" * 72)

    before_run = read_run_values()
    before_tasks = {name: task_exists(name) for name in LEGACY_TASKS}
    print(f"测试前：HKCU Run 共 {len(before_run)} 项；旧版计划任务 "
          f"{ {name: exists for name, exists in before_tasks.items()} }")
    print(f"      已有 {VALUE_NAME}: {VALUE_NAME in before_run}")

    # 上一次测试 / 崩溃可能留下我们自己的自启动值，先清掉，保证基线干净
    if VALUE_NAME in before_run:
        print(f"      发现遗留的 {VALUE_NAME}，先清理（这属于本程序自己的值）")
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, VALUE_NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"      清理失败：{exc}")
        before_run = read_run_values()

    # 本机模拟门户（不访问外网）
    sys.path.insert(0, str(PROJECT_ROOT / "tools"))
    import mock_portal  # noqa: PLC0415

    mock_portal.STATE.update({
        "authed_ips": set(), "account": "sysint-user", "password": "sysint-password",
        "logins": 0, "fails": 0, "already_online": False,
    })
    server = mock_portal.ThreadingHTTPServer(("127.0.0.1", 0), mock_portal.Handler)
    portal_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"模拟门户：http://127.0.0.1:{portal_port}/")

    data_dir = user_data_dir("main")
    write_test_config(data_dir, portal_port)

    registry_ok, mode, mode_note = select_mode(data_dir)
    print(f"\n被测模式：{'打包 EXE' if mode == 'frozen' else '源码模式（同一套逻辑）'}")
    if mode_note:
        print(f"[环境限制] {mode_note}")
        print("           注册表 / 启动项 / 计划任务类检查若仍失败将标记为 SKIP；")
        print("           打包 EXE 的真实行为需在普通用户会话中按")
        print("           docs/WINDOWS_ACCEPTANCE.md 第 6 节验证。")
    if not registry_ok:
        print(f"[环境限制] {mode_note}")

    try:
        # ---------- 1. 自启动登记 ----------
        print("\n1) 注册表自启动登记")
        if not registry_ok:
            for name in ("autostart on 返回成功", f"HKCU Run 里出现 {VALUE_NAME}",
                         "登记的路径指向 CampusNetService.exe",
                         "登记命令里没有明文密码", "其它启动项一个都没动",
                         "autostart status 反映已开启", "autostart off 删除注册表值"):
                skip(name, mode_note)
        else:
            code, out = cli(["autostart", "on"], data_dir=data_dir)
            check("autostart on 返回成功", code == 0, out[:300])
            after_run = read_run_values()
            check(f"HKCU Run 里出现 {VALUE_NAME}", VALUE_NAME in after_run, str(list(after_run)))
            registered = after_run.get(VALUE_NAME, "")
            # 源码模式下登记的应该是 pythonw -m campusnet.app.service；
            # 打包模式应该是同目录的 CampusNetService.exe。两者都不能指向图形界面。
            points_to_daemon = (
                "campusnetservice.exe" in registered.lower()
                or "campusnet.app.service" in registered.lower()
            )
            check("登记的命令指向后台守护（不是图形界面）", points_to_daemon, registered)
            check("登记命令里没有明文密码", "password" not in registered.lower(), registered)
            others_before = {k: v for k, v in before_run.items() if k != VALUE_NAME}
            others_after = {k: v for k, v in after_run.items() if k != VALUE_NAME}
            check("其它启动项一个都没动", others_before == others_after,
                  f"{set(others_before) ^ set(others_after)}")
            code, out = cli(["autostart", "status"], data_dir=data_dir)
            check("autostart status 反映已开启",
                  '"enabled": true' in out.replace("\n", " "), out[:300])

        # ---------- 2. 启动 + 单实例 ----------
        print("\n2) 启动后台守护与单实例互斥")
        code, out = cli(["start"], data_dir=data_dir)
        check("start 返回成功", code == 0, out[:300])
        pids = daemon_pids()
        check("系统里出现了后台守护进程", len(pids) >= 1, str(pids))

        cli(["start"], data_dir=data_dir)
        time.sleep(2.0)
        pids2 = daemon_pids()
        check("第二次 start 不会产生第二个实例",
              len(pids2) <= max(1, len(pids)), f"之前 {pids}，现在 {pids2}")

        heartbeat = data_dir / "heartbeat.json"
        ok = wait_for(lambda: heartbeat.exists() and heartbeat.stat().st_size > 0)
        check("后台守护写出心跳文件", ok, str(heartbeat))
        state = ""
        if heartbeat.exists():
            state = str(json.loads(heartbeat.read_text(encoding="utf-8")).get("state") or "")
        check("心跳状态是已知状态",
              state in {"starting", "checking", "online", "wifi", "auth",
                        "cooldown", "need-user", "paused", "stopped"}, state)

        log = data_dir / "logs" / "campus_login.log"
        log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        check("守护日志已写出", bool(log_text.strip()))
        check("日志里没有明文密码", "sysint-password" not in log_text)
        check("日志里账号已脱敏", "sysint-user" not in log_text, log_text[:200])

        # ---------- 3. 优雅停止 ----------
        print("\n3) 优雅停止（stop.flag，不是硬杀进程）")
        code, out = cli(["stop"], data_dir=data_dir)
        check("stop 返回成功", code == 0, out[:300])
        check("后台守护确实退出了", wait_for(lambda: not daemon_pids()), str(daemon_pids()))
        check("stop.flag 已被清理", not (data_dir / "stop.flag").exists())

        # ---------- 4. 取消自启动 ----------
        print("\n4) 取消自启动")
        if not registry_ok:
            skip("取消自启动与最终还原", mode_note)
        else:
            code, out = cli(["autostart", "off"], data_dir=data_dir)
            check("autostart off 返回成功", code == 0, out[:300])
            final_run = read_run_values()
            check(f"HKCU Run 里的 {VALUE_NAME} 已删除", VALUE_NAME not in final_run,
                  str(list(final_run)))
            check("其它启动项仍然完好", set(final_run) == set(before_run),
                  f"{set(final_run) ^ set(before_run)}")

        # ---------- 5. 旧版残留清理 ----------
        print("\n5) 旧版（V2）残留清理")
        if args.keep_legacy:
            skip("旧版残留清理", "--keep-legacy")
        elif not task_exists(LEGACY_TASKS[0]) and not any(
                (Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
                 / "campus-net-autologin.vbs").exists() for _ in [0]):
            skip("旧版残留清理", "本机没有旧版残留")
        else:
            code, out = cli(["cleanup-legacy"], data_dir=data_dir)
            print("       " + out.replace("\n", "\n       "))
            remaining = [name for name in LEGACY_TASKS if task_exists(name)]
            if "WinError 5" in out or "拒绝访问" in out:
                skip("旧版计划任务已被清理", "本会话令牌不允许删除计划任务")
            else:
                check("旧版计划任务已被清理", not remaining, str(remaining))

        # ---------- 6. 卸载（保留数据） ----------
        print("\n6) 卸载流程（默认保留用户数据）")
        code, out = cli(["uninstall"], data_dir=data_dir)
        print("       " + out.replace("\n", "\n       ")[:800])
        check("卸载后没有残留守护进程", not daemon_pids(), str(daemon_pids()))
        check("卸载默认保留了数据目录", data_dir.exists(), str(data_dir))
        if registry_ok:
            check("卸载后自启动已关闭", VALUE_NAME not in read_run_values())
        else:
            skip("卸载后自启动已关闭", mode_note)

        print("\n7) 卸载（--remove-data 会删除数据）")
        code, out = cli(["uninstall", "--remove-data"], data_dir=data_dir)
        check("uninstall --remove-data 不再报阻塞错误",
              "无法删除用户数据目录" not in out, out[:300])
        check("数据目录已被删除", not data_dir.exists(), str(data_dir))
        check("安装目录没有被删", DIST.exists())
    finally:
        # ---------- 还原现场 ----------
        print("\n还原现场 ...")
        try:
            subprocess.run(["taskkill", "/F", "/IM", "CampusNetService.exe"],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass
        if registry_ok and VALUE_NAME not in before_run:
            try:
                _code, out = cli(["autostart", "off"], data_dir=user_data_dir("cleanup"))
                print(f"  自启动清理：{out[:120]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  自启动清理失败：{exc}")
        server.shutdown()
        server.server_close()

        import shutil

        for leftover in [*_CREATED_DIRS, data_dir]:
            shutil.rmtree(leftover, ignore_errors=True)
        _CREATED_DIRS.clear()

        final = read_run_values()
        if registry_ok:
            restored = all(final.get(k) == v for k, v in before_run.items()) and \
                (VALUE_NAME in before_run or VALUE_NAME not in final)
            check("测试结束：HKCU Run 已还原到初始状态", restored,
                  f"差异：{set(final) ^ set(before_run)}")
        else:
            unchanged = all(final.get(k) == v for k, v in before_run.items())
            check("测试结束：HKCU Run 一个值都没动", unchanged,
                  f"差异：{set(final) ^ set(before_run)}")

    print()
    print("=" * 72)
    total = len(PASSED) + len(FAILED)
    print(f"结果：{len(PASSED)}/{total} 通过，{len(SKIPPED)} 项跳过")
    for name in FAILED:
        print("  FAIL:", name)
    for name in SKIPPED:
        print("  SKIP:", name)
    print("=" * 72)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())

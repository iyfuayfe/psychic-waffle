"""本机端到端认证测试：真 HTTP + 真 DPAPI + 真 EXE，全程不联网。

为什么需要它：单元测试用假 client，验证不了"整条链路真的能登录"。
这里起一个本机模拟门户（`tools/mock_portal.py`，Dr.COM / 哆点风格，GBK 页面、
无 form、参数内嵌在 JS 里、登录接口返回 JSONP），然后用**真实配置 + 真实 DPAPI
密文**驱动认证，检查：

    探测到跳转 -> 识别 Dr.COM -> 提交 -> 门户真的记录了登录 -> 复核联网通过

被测主体可以是源码模式，也可以是打包后的 ``CampusNetService.exe``（默认两者都测）。

用法：
    python tools/test_local_e2e.py                 # 源码模式 + 打包 EXE 都测
    python tools/test_local_e2e.py --source-only
    python tools/test_local_e2e.py --frozen-only
    python tools/test_local_e2e.py -v              # 打印完整日志
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import mock_portal  # noqa: E402

from campusnet import crypto  # noqa: E402

FAILED: list = []
PASSED: list = []

USER = "testuser"
SUFFIX = "@cmcc"
PASSWORD = "testpass123"


def check(name: str, ok: bool, detail: str = "") -> bool:
    if ok:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return ok


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_portal(port: int):
    """在后台线程起模拟门户。"""
    mock_portal.STATE.update({
        "authed_ips": set(),
        "account": USER + SUFFIX,
        "password": PASSWORD,
        "logins": 0,
        "fails": 0,
        "already_online": False,
    })
    server = mock_portal.ThreadingHTTPServer(("127.0.0.1", port), mock_portal.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def workspace_temp(prefix: str) -> Path:
    """在工作区里造临时目录。

    刻意不用系统 ``%TEMP%``：在受限账户 / 沙箱里，那里建出来的目录 ACL 可能
    让被测 EXE 无法在其中创建子目录（实测表现为"认证成功但没有日志、没有
    state.json"）。工作区目录继承的权限是正常的。

    所有临时目录都会登记到 ``_TEMP_DIRS``，脚本结束时统一删除（测试不该留垃圾）。
    """
    root = PROJECT_ROOT / ".tmp"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{prefix}-{int(time.time() * 1000)}-{os.getpid()}"
    target.mkdir(parents=True, exist_ok=True)
    _TEMP_DIRS.append(target)
    return target


_TEMP_DIRS: list = []


def cleanup_temp_dirs() -> None:
    import shutil

    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)
    _TEMP_DIRS.clear()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def http_get(url: str, timeout: float = 5.0, follow: bool = False):
    """GET 一个地址；默认**不跟随重定向**，这样才能断言"未认证时是 302"。"""
    opener = urllib.request.urlopen if follow else _NO_REDIRECT_OPENER.open
    try:
        with opener(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc).encode()


def write_config(data_dir: Path, port: int, *, password: str, drcom_enabled: bool = True) -> Path:
    """造一份"和真实用户一样"的配置：密码是 DPAPI 密文。"""
    cfg = {
        "config_version": 3,
        "username": USER,
        "password_enc": crypto.protect(password),
        "wifi_ssid": "",
        "auto_enabled": True,
        "portal_auth_enabled": True,
        "autofill_click": True,
        "request_timeout": 5,
        "interval_offline": 3,
        "interval_online": 10,
        "interval_max": 20,
        "log_level": "INFO",
        # 只用本机模拟门户当探测目标，绝不访问外网
        "check_urls": [
            {"url": f"http://127.0.0.1:{port}/connecttest.txt",
             "expect_text": "Microsoft Connect Test"}
        ],
        "drcom": {
            "enabled": drcom_enabled,
            "host": "127.0.0.1",
            "port": port,
            "account_suffix": SUFFIX,
            "wlan_user_ip": "",
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "wlan_user_mac": "000000000000",
            "js_version": "",
            "login_method": -1,
            "terminal_type": 1,
        },
        "custom": {"enabled": False, "url": "", "method": "POST"},
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_service(command: list, data_dir: Path, args: list, timeout: float = 120.0):
    env = dict(os.environ)
    env["CAMPUSNET_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.time()
    proc = subprocess.run(
        [*command, *args],
        capture_output=True,
        timeout=timeout,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    return proc, time.time() - started


def read_log(data_dir: Path) -> str:
    log_dir = data_dir / "logs"
    candidates = []
    if log_dir.exists():
        candidates.extend(sorted(log_dir.glob("campus_login.log*")))
    candidates.append(log_dir / "campus_login.log")
    for path in candidates:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
    return ""


def scenario_success(label: str, command: list, verbose: bool) -> None:
    """成功路径：探测到门户 -> 认证 -> 复核通过。"""
    print(f"\n[{label}] 成功路径")
    port = free_port()
    server, thread = start_portal(port)
    tmp = workspace_temp(f"e2e-{label}")
    try:
        write_config(tmp, port, password=PASSWORD)

        status, body = http_get(f"http://127.0.0.1:{port}/connecttest.txt")
        check(f"{label}: 未认证时探测目标返回 302", status == 302, f"实际 {status}")

        proc, elapsed = run_service(command, tmp, ["--once"], timeout=180)
        log = read_log(tmp)
        if verbose:
            print("    ---- 日志 ----")
            print("    " + log.replace("\n", "\n    ").rstrip())

        check(f"{label}: --once 退出码 0（认证成功）", proc.returncode == 0,
              f"退出码 {proc.returncode}；stderr={proc.stderr[:300]!r}")
        check(f"{label}: 日志记录了认证成功", "认证成功" in log, log[-400:])
        check(f"{label}: 门户真的收到并接受了这次登录",
              mock_portal.STATE["logins"] >= 1, f"logins={mock_portal.STATE['logins']}")
        check(f"{label}: 认证后探测目标恢复 200",
              http_get(f"http://127.0.0.1:{port}/connecttest.txt")[0] == 200)
        check(f"{label}: 日志里没有明文密码", PASSWORD not in log)
        check(f"{label}: 日志里账号已脱敏", USER + SUFFIX not in log, "出现了完整账号")

        state_path = tmp / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        check(f"{label}: 记住了可用策略", bool(state.get("last_strategy")),
              str(state))
        if verbose:
            print(f"    last_strategy = {state.get('last_strategy')!r}，耗时 {elapsed:.1f}s")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def scenario_bad_password(label: str, command: list, verbose: bool) -> None:
    """凭据错误：必须明确报"账号或密码"，且不反复撞锁。"""
    print(f"\n[{label}] 密码错误路径")
    port = free_port()
    server, thread = start_portal(port)
    tmp = workspace_temp(f"e2e-bad-{label}")
    try:
        write_config(tmp, port, password="wrong-password-123")
        proc, _elapsed = run_service(command, tmp, ["--once"], timeout=180)
        log = read_log(tmp)
        if verbose:
            print("    ---- 日志 ----")
            print("    " + log.replace("\n", "\n    ").rstrip())

        check(f"{label}: 密码错误时 --once 非 0 退出", proc.returncode != 0,
              f"退出码 {proc.returncode}")
        check(f"{label}: 日志指出是账号/密码问题",
              ("账号或密码" in log) or ("密码问题" in log) or ("凭据" in log), log[-400:])
        check(f"{label}: 没有把错误密码写进日志", "wrong-password-123" not in log)
        check(f"{label}: 门户记录了失败尝试", mock_portal.STATE["fails"] >= 1,
              f"fails={mock_portal.STATE['fails']}")
        check(f"{label}: 认证失败后探测目标仍是 302",
              http_get(f"http://127.0.0.1:{port}/connecttest.txt")[0] == 302)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def scenario_portal_unreachable(label: str, command: list) -> None:
    """门户不可达：要报网络问题，而不是"认证失败"。"""
    print(f"\n[{label}] 网络不可达路径")
    port = free_port()          # 只取端口，不启动服务器
    tmp = workspace_temp(f"e2e-net-{label}")
    write_config(tmp, port, password=PASSWORD)
    proc, _elapsed = run_service(command, tmp, ["--check"], timeout=120)
    log = read_log(tmp)
    check(f"{label}: --check 在断网/不可达时退出码为 1", proc.returncode == 1,
          f"退出码 {proc.returncode}")
    check(f"{label}: 状态分类到网络层（unreachable/timeout/refused）",
          any(kind in log for kind in ("unreachable", "timeout", "refused", "dns-error")),
          log[-300:])


def scenario_drcom_disabled(label: str, command: list) -> None:
    """关闭 Dr.COM 适配器后应落到通用表单，并明确报告"找不到登录表单"。"""
    print(f"\n[{label}] 不支持的门户（关闭 Dr.COM 适配）")
    port = free_port()
    server, thread = start_portal(port)
    tmp = workspace_temp(f"e2e-nodrcom-{label}")
    try:
        write_config(tmp, port, password=PASSWORD, drcom_enabled=False)
        proc, _elapsed = run_service(command, tmp, ["--once"], timeout=180)
        log = read_log(tmp)
        check(f"{label}: 通用表单找不到登录框时明确报告", "找不到登录表单" in log, log[-400:])
        check(f"{label}: 没有谎报成功", proc.returncode != 0, f"退出码 {proc.returncode}")
        check(f"{label}: 报告了页面标题便于定位", "上网登录页" in log, log[-300:])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机端到端认证测试")
    parser.add_argument("--source-only", action="store_true")
    parser.add_argument("--frozen-only", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--skip-negative", action="store_true", help="只跑成功路径")
    args = parser.parse_args(argv)

    if not crypto.available():
        print("[错误] 当前系统不支持 DPAPI，无法做端到端测试")
        return 2

    targets: list[tuple[str, list]] = []
    if not args.frozen_only:
        targets.append(("源码模式", [sys.executable, "-m", "campusnet.app.service"]))
    if not args.source_only:
        exe = PROJECT_ROOT / "dist" / "CampusNet" / "CampusNetService.exe"
        if exe.exists():
            targets.append(("打包 EXE", [str(exe)]))
        else:
            print("[提示] 没有 dist\\CampusNet\\CampusNetService.exe，跳过打包 EXE 测试")

    print("=" * 72)
    print("CampusNet 本机端到端认证测试（模拟门户，不访问外网）")
    print("=" * 72)

    for label, command in targets:
        scenario_success(label, command, args.verbose)
        if args.skip_negative:
            continue
        scenario_bad_password(label, command, args.verbose)
        scenario_portal_unreachable(label, command)
        scenario_drcom_disabled(label, command)

    cleanup_temp_dirs()

    print()
    print("=" * 72)
    total = len(PASSED) + len(FAILED)
    print(f"结果：{len(PASSED)}/{total} 项通过")
    if FAILED:
        for name in FAILED:
            print("  FAIL:", name)
    print("=" * 72)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())

"""首次配置向导（手工运行一次，控制台交互）。

做四件事：
  1. 自动探测认证页地址（也可以 --portal-url 直接给）
  2. 交互式输入账号密码（密码用 getpass，不回显、不落盘明文）
  3. 用 Windows DPAPI 加密密码后写入 config.json
  4. 注册计划任务，实现"登录即自动后台运行"

全自动用法（不交互）：
    python tools/setup_wizard.py --username 你的账号 --password 你的密码 --yes
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campusnet import crypto  # noqa: E402
from campusnet.authenticator import PortalAuthenticator  # noqa: E402
from campusnet.checker import check  # noqa: E402
from campusnet.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Config  # noqa: E402
from campusnet.daemon import build_client, make_verifier  # noqa: E402
from campusnet.drcom import looks_like_drcom  # noqa: E402
from campusnet.http import HttpClient  # noqa: E402
from campusnet.logs import setup_logging  # noqa: E402
from campusnet.portal import PortalPage  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gui_common import install_task, whitelist_status  # noqa: E402

DEFAULT_PORTAL = "http://111.17.200.130/"
TASK_NAME = "CampusNetAutoLogin"


def out(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def ask(prompt: str, default: str = "") -> str:
    tip = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{tip}: ").strip()
    except EOFError:
        value = ""
    return value or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    tip = "Y/n" if default else "y/N"
    try:
        value = input(f"{prompt} [{tip}]: ").strip().lower()
    except EOFError:
        return default
    if not value:
        return default
    return value in ("y", "yes", "是", "1")


def detect_portal(client: HttpClient, cfg: Config) -> tuple[str, str]:
    """返回 (portal_url, 说明)。"""
    try:
        result = check(client, cfg)
    except Exception as exc:  # noqa: BLE001
        return "", f"探测失败：{exc}"
    if result.portal_url:
        return result.portal_url, f"探测到认证跳转：{result.portal_url}"
    if result.online:
        return "", "当前已在线，没有认证跳转（可稍后掉线再跑一次，或直接填地址）"
    return "", f"未探测到认证跳转（{result.detail}）"


def sniff_account(portal_url: str, client: HttpClient) -> tuple[str, str]:
    """从门户页里嗅探当前在线账号和后缀，用于预填。"""
    try:
        resp = client.get(portal_url, follow_redirects=True)
        html = resp.text
    except Exception:  # noqa: BLE001
        return "", ""
    uid = ""
    m = re.search(r"uid\s*=\s*'([^']+)'", html)
    if m:
        uid = m.group(1)
    if not uid:
        m = re.search(r"uid\s*=\s*\"([^\"]+)\"", html)
        if m:
            uid = m.group(1)
    suffix = ""
    if "@" in uid:
        suffix = "@" + uid.split("@", 1)[1]
    account = uid.split("@", 1)[0] if uid else ""
    return account, suffix


def sniff_drcom(portal_url: str, client: HttpClient) -> dict:
    info = {"is_drcom": False, "port": 801}
    try:
        resp = client.get(portal_url, follow_redirects=True)
    except Exception:  # noqa: BLE001
        return info
    page = PortalPage(resp.text, resp.url)
    info["is_drcom"] = looks_like_drcom(resp.url, page)
    m = re.search(r"authloginport\s*=\s*(\d+)", page.html or "")
    if m:
        try:
            info["port"] = int(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    return info


def harden(path: Path) -> None:
    """尽量收紧配置文件权限（失败不影响功能，密码本身已 DPAPI 加密）。"""
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{_me()}:F"],
            capture_output=True, timeout=15, check=False,
        )
    except Exception:  # noqa: BLE001
        pass


def _me() -> str:
    import os

    return os.environ.get("USERNAME", "") or ""


# install_task 已移到 gui_common.py（GUI 与命令行共用，避免两份实现走偏）


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校园网自动登录 - 首次配置向导")
    ap.add_argument("--portal-url", default="")
    ap.add_argument("--username", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--suffix", default="", help="账号后缀，如 @cmcc / @dx / @lt")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--no-task", action="store_true", help="不注册计划任务")
    ap.add_argument("--no-verify", action="store_true", help="不尝试真实验证")
    ap.add_argument("--yes", action="store_true", help="全部使用默认值，不交互")
    args = ap.parse_args(argv)

    config_path = Path(args.config)
    interactive = not args.yes and sys.stdin is not None and not sys.stdin.closed
    if args.yes and not (args.username and args.password):
        out("[错误] --yes 模式下必须同时提供 --username 和 --password")
        return 2

    try:
        cfg = Config.load(config_path)
    except Exception:  # noqa: BLE001
        cfg = Config()

    out("=" * 64)
    out("  校园网自动登录 - 配置向导（后台静默运行，不会弹任何窗口）")
    out("=" * 64)
    out()

    client = build_client(cfg)

    # ---- 1. 认证页地址 ----
    portal_url = args.portal_url or cfg.portal_url
    if not portal_url:
        out("[1/5] 探测认证页地址 ...")
        detected, why = detect_portal(client, cfg)
        out(f"      {why}")
        if not detected:
            detected = DEFAULT_PORTAL
            out(f"      使用默认地址：{detected}")
        portal_url = detected
        if interactive:
            portal_url = ask("      确认认证页地址（回车接受）", portal_url)
    else:
        out(f"[1/5] 认证页地址：{portal_url}")

    drcom_info = sniff_drcom(portal_url, client)
    if drcom_info["is_drcom"]:
        out(f"      识别为 Dr.COM / 哆点 ePortal（认证端口 {drcom_info['port']}）")

    # ---- 2. 账号 ----
    sniffed_account, sniffed_suffix = sniff_account(portal_url, client)
    out()
    out("[2/5] 上网账号")
    username = args.username or cfg.username or sniffed_account
    if sniffed_account:
        out(f"      从门户页读到当前在线账号：{sniffed_account}（用于预填）")
    if interactive:
        username = ask("      请输入账号", username)
    if not username:
        out("[错误] 账号不能为空")
        return 2

    suffix = args.suffix or cfg.drcom.account_suffix or sniffed_suffix
    if drcom_info["is_drcom"]:
        if interactive and not args.suffix:
            suffix = ask("      账号后缀（校园用户不填；@dx=电信 @lt=联通，没有就回车）", suffix)
        if suffix:
            out(f"      账号后缀：{suffix}")

    # ---- 3. 密码 ----
    out()
    out("[3/5] 上网密码（输入时不显示，只在本机加密保存）")
    password = args.password
    if not password and interactive:
        while True:
            p1 = getpass.getpass("      请输入密码: ")
            if not p1:
                out("      密码不能为空，请重试")
                continue
            p2 = getpass.getpass("      再输一次确认: ")
            if p1 != p2:
                out("      两次输入不一致，请重试")
                continue
            password = p1
            break
    if not password:
        out("[错误] 密码不能为空")
        return 2

    # ---- 4. 加密并写配置 ----
    out()
    out("[4/5] 加密并写入配置")
    try:
        enc = crypto.protect(password)
    except Exception as exc:  # noqa: BLE001
        out(f"[错误] DPAPI 加密失败：{exc}")
        return 3

    cfg.portal_url = portal_url
    cfg.username = username
    cfg.password_enc = enc
    cfg.drcom.enabled = True
    if drcom_info["is_drcom"] or "111.17.200.130" in portal_url:
        cfg.drcom.host = cfg.drcom.host or _host_of(portal_url)
        cfg.drcom.port = int(drcom_info["port"] or 801)
        cfg.drcom.account_suffix = suffix
    try:
        cfg.save(config_path)
    except Exception as exc:  # noqa: BLE001
        out(f"[错误] 配置写入失败：{exc}")
        return 3
    harden(config_path)
    out(f"      已写入：{config_path}")
    out(f"      密码以 DPAPI 密文保存（换 Windows 用户或换电脑都解不开）")

    # ---- 5. 验证 ----
    if not args.no_verify:
        out()
        out("[5/5] 验证一次（当前已在线的话会直接提示在线，属正常）")
        logger = setup_logging(cfg, console=False)
        state = check(client, cfg)
        if state.online:
            out("      当前网络已在线，无需认证 —— 链路正常。")
        else:
            portal = cfg.portal_url or state.portal_url
            auth = PortalAuthenticator(client, cfg, logger)
            result = auth.login(portal, username, password, make_verifier(client, cfg, logger, tries=1))
            if result.ok:
                out(f"      认证成功：{result.reason}")
            else:
                out(f"      [提示] 本次认证未成功：{result.reason}")
                out("      若现在本来就是在线状态，这条可以忽略；")
                out("      等真的掉线时再运行：python campus_login.py --once --verbose")
    else:
        out()
        out("[5/5] 已跳过验证")

    # ---- 计划任务 ----
    out()
    if args.no_task:
        out("[任务] 已跳过开机自启注册。手动启动方式：")
        out(f'      pythonw.exe "{PROJECT_ROOT / "campus_login.py"}"')
    else:
        do_install = True
        if interactive:
            do_install = ask_yes("[任务] 现在注册开机自动后台运行吗？", True)
        if do_install:
            ok, info = install_task(Path(__file__).resolve().parent)
            out("[任务] 注册" + ("成功" if ok else "失败"))
            if info:
                out("      " + info.replace("\n", "\n      "))
            if not ok:
                out("      可稍后手动执行：powershell -ExecutionPolicy Bypass -File tools\\install_task.ps1")
        else:
            out("[任务] 已跳过")

    # ---- 杀毒白名单提醒 ----
    try:
        wl = whitelist_status()
        need_wl = bool(
            (wl.get("defender_available") and (wl.get("missing_processes") or wl.get("missing_paths")))
            or (not wl.get("defender_available") and wl.get("third_party_av"))
        )
    except Exception:  # noqa: BLE001
        need_wl = True
    if need_wl:
        out()
        out("[提示] 建议把程序加入杀毒软件白名单，防止被杀毒软件误拦导致偶尔不生效：")
        out("       双击「4-加入白名单.bat」（自动处理 Windows Defender；")
        out("       360 / 火绒 / 电脑管家等第三方杀毒会打印手动添加步骤）")

    out()
    out("=" * 64)
    out("完成。日志在 logs\\campus_login.log，全程无窗口、无弹窗。")
    out("手动排障：python campus_login.py --once --verbose")
    out("=" * 64)
    return 0


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


if __name__ == "__main__":
    sys.exit(main())

"""后台守护主循环。

"不弹窗"的落地方式：
- 进程用 pythonw.exe 启动 => 没有控制台窗口；
- 所有异常就地捕获写日志，绝不让 Python 的未捕获异常弹出崩溃对话框；
- 日志只写文件；
- 子进程调用一律带 CREATE_NO_WINDOW。
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

from . import crypto
from .authenticator import PortalAuthenticator
from .checker import CheckResult, check
from .config import DEFAULT_CONFIG_PATH, LOG_DIR, Config
from .http import HttpClient
from .logs import setup_logging
from .notify import notify

FAIL_LOG_INTERVAL = 300  # 持续失败时，最多每 5 分钟记一条，避免日志爆炸


def build_client(cfg: Config) -> HttpClient:
    return HttpClient(
        timeout=cfg.request_timeout,
        insecure_tls=bool(cfg.insecure_tls),
        use_system_proxy=bool(cfg.use_system_proxy),
        user_agent=cfg.user_agent,
    )


def _sleep(seconds: float) -> None:
    try:
        time.sleep(max(1.0, float(seconds)))
    except Exception:  # noqa: BLE001
        pass


def make_verifier(client: HttpClient, cfg: Config, log, tries: int = 3):
    def verify() -> bool:
        for i in range(tries):
            try:
                if check(client, cfg).online:
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("验证时探测异常：%s", exc)
            if i < tries - 1:
                time.sleep(1.2)
        return False

    return verify


def load_credential(cfg: Config, log):
    if not cfg.is_configured():
        log.error("配置不完整：缺少 username 或 password_enc。请先运行 tools/setup_wizard.py")
        return None
    try:
        return crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        log.error("密码解密失败：%s（多半是换了 Windows 用户或换了电脑，请重新运行配置向导）", exc)
        return None


def run_once(cfg: Config, client: HttpClient, log) -> int:
    password = load_credential(cfg, log)
    if password is None:
        return 3
    state = check(client, cfg)
    if state.online:
        log.info("网络已在线，无需认证（%s）", state.detail)
        return 0
    log.info("当前无法上网：原因=%s，详情=%s", state.reason, state.detail)
    # 注意取用顺序：优先**本次现场探测到的**跳转地址，配置里的 portal_url 只是兜底。
    # （配置里写死的地址会带上当时的 wlanuserip，过期后会导致服务端认不出会话）
    portal = state.portal_url or cfg.portal_url
    if not portal:
        log.error("没有可用的认证页地址，请运行 detect_portal.py 探测后写入 config.json")
        return 2
    auth = PortalAuthenticator(client, cfg, log)
    result = auth.login(portal, cfg.username, password, make_verifier(client, cfg, log, tries=1))
    if result.ok:
        log.info("认证成功：%s", result.reason)
        return 0
    log.error("认证失败：%s", result.reason)
    return 1


def run_forever(cfg: Config, client: HttpClient, log, config_path: Path) -> int:
    password = load_credential(cfg, log)
    if password is None:
        return 3

    auth = PortalAuthenticator(client, cfg, log)
    verify = make_verifier(client, cfg, log)
    log.info(
        "后台守护启动：portal=%s，在线巡检 %ss，掉线重试 %ss",
        cfg.portal_url or "(自动探测)",
        cfg.interval_online,
        cfg.interval_offline,
    )

    last_online: bool | None = None
    interval = float(cfg.interval_offline)
    last_fail_log = 0.0
    had_failure = False
    notified_bad = False
    cfg_mtime = _mtime(config_path)

    while True:
        try:
            # 配置热更新：用户重新跑了配置向导，这里不必重启进程
            new_mtime = _mtime(config_path)
            if new_mtime and new_mtime != cfg_mtime:
                cfg_mtime = new_mtime
                try:
                    new_cfg = Config.load(config_path)
                    if new_cfg.is_configured():
                        if new_cfg.password_enc != cfg.password_enc:
                            new_password = crypto.unprotect(new_cfg.password_enc)
                            if new_password:
                                password = new_password
                        cfg = new_cfg
                        client = build_client(cfg)
                        auth = PortalAuthenticator(client, cfg, log)
                        verify = make_verifier(client, cfg, log)
                        log.info("检测到配置更新，已热加载")
                except Exception as exc:  # noqa: BLE001
                    log.warning("配置热加载失败，继续用旧配置：%s", exc)

            state: CheckResult = check(client, cfg)

            if state.online:
                if last_online is False:
                    log.info("网络已恢复，恢复常规巡检")
                last_online = True
                interval = float(cfg.interval_online)
            else:
                if last_online is not False:
                    log.warning("无法上网：原因=%s，详情=%s", state.reason, state.detail)
                last_online = False

                portal = state.portal_url or cfg.portal_url
                if not portal:
                    now = time.monotonic()
                    if now - last_fail_log >= FAIL_LOG_INTERVAL:
                        last_fail_log = now
                        log.warning(
                            "未探测到认证页跳转（%s）。若是网线/WiFi 未连接属正常；"
                            "若已连上校园网，请运行 detect_portal.py 并填写 portal_url",
                            state.detail,
                        )
                else:
                    detail = state.detail
                    result = auth.login(portal, cfg.username, password, verify)
                    now = time.monotonic()
                    if result.ok:
                        log.info("自动认证成功（%s，尝试 %d 次）", result.reason, result.attempts)
                        if had_failure and cfg.notify_on_success:
                            notify(cfg, f"网络已恢复，自动重新登录成功。\n{result.reason}", log)
                        last_fail_log = 0.0
                        had_failure = False
                        notified_bad = False
                        interval = float(cfg.interval_online)
                        last_online = True
                    else:
                        if not had_failure:
                            log.warning(
                                "自动认证未成功。若之后仍反复失败，可能是杀毒软件拦截了后台程序："
                                "建议双击「4-加入白名单.bat」把程序加入杀毒白名单。"
                            )
                        had_failure = True
                        if now - last_fail_log >= FAIL_LOG_INTERVAL or result.bad_credentials:
                            last_fail_log = now
                            log.error("自动认证未成功：%s（探测详情：%s）", result.reason, detail)
                        if result.bad_credentials:
                            # 账号密码不对：重试没有意义，别把账号撞锁；只在第一次推一条通知
                            interval = float(cfg.interval_max)
                            if not notified_bad:
                                notified_bad = True
                                notify(cfg, f"自动登录失败：账号或密码有问题。\n{result.reason}\n请重新运行配置向导。", log)
                        else:
                            interval = min(interval * 1.6, float(cfg.interval_max))

            _sleep(interval + random.uniform(0, max(1.0, interval * 0.15)))

        except KeyboardInterrupt:  # pragma: no cover
            log.info("收到中断信号，退出")
            return 0
        except Exception as exc:  # noqa: BLE001  任何异常都不允许冒泡
            log.exception("主循环异常，5 秒后继续：%s", exc)
            _sleep(5)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return 0.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="campus_login.py",
        description="校园网后台自动登录（静默、无弹窗）",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只检查/认证一次就退出（排障用）")
    parser.add_argument("--verbose", action="store_true", help="同时把日志打到控制台（仅排障用）")
    parser.add_argument("--check", action="store_true", help="只检查联网状态，不做认证")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)

    try:
        cfg = Config.load(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[致命] {exc}", file=sys.stderr)
        return 4

    console = bool(args.verbose) and os.environ.get("CAMPUSNET_SILENT") != "1"
    log = setup_logging(cfg, console=console)

    if args.check:
        client = build_client(cfg)
        state = check(client, cfg)
        log.info("状态：%s（原因=%s）%s", "在线" if state.online else "离线", state.reason, state.detail)
        if console:
            print(f"online={state.online} reason={state.reason} portal={state.portal_url or '-'} detail={state.detail}")
        return 0 if state.online else 1

    client = build_client(cfg)

    if args.once:
        return run_once(cfg, client, log)

    handle = crypto.acquire_single_instance()
    if handle is None:
        log.info("已有后台实例在运行，本进程退出")
        return 0
    try:
        return run_forever(cfg, client, log, config_path)
    except Exception as exc:  # noqa: BLE001 最后一道防线
        log.exception("守护进程异常退出：%s", exc)
        return 1


def cli() -> None:  # pragma: no cover
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        code = 1
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    cli()

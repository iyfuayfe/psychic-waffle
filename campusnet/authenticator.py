"""认证执行器：把账号密码送进校园网 Portal，并验证是否真的通了。

策略级联（每层都用**真实联网结果**判定成败，而不是只看返回文字）：
  1. custom 显式接口                 —— 用户手工指定的逃生舱
  2. Dr.COM / 哆点 ePortal 专用策略  —— 针对 111.17.200.130 这类门户逆向实现，
                                        内部还有 5 个子策略会逐个试
  3. 通用 HTML 表单自动识别          —— 覆盖绝大多数其他高校
成功的那套策略名会记进 state.json，下次优先复用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import LOG_DIR, Config, load_state, save_state
from .drcom import DrcomAdapter, looks_like_drcom
from .http import HttpClient
from .portal import PortalPage, build_payload, summarize

Verifier = Callable[[], bool]


@dataclass
class LoginResult:
    ok: bool
    reason: str = ""
    attempts: int = 0
    bad_credentials: bool = False
    strategy: str = ""
    dump_path: str = ""


class PortalAuthenticator:
    def __init__(self, client: HttpClient, cfg: Config, logger):
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.drcom = DrcomAdapter(client, cfg, logger)

    # ---------- 对外入口 ----------

    def login(self, portal_url: str, username: str, password: str, verify: Verifier) -> LoginResult:
        attempts = 0
        state = load_state()
        preferred = str(state.get("last_strategy", "") or "")

        # ---- 1. custom ----
        if self.cfg.custom.enabled and self.cfg.custom.url:
            attempts += 1
            ok, reason, bad = self._custom_login(username, password, verify)
            if ok:
                self._remember("custom")
                return LoginResult(True, "custom 接口认证成功", attempts, strategy="custom")
            self.log.warning("custom 接口认证失败：%s", reason)
            if bad:
                return LoginResult(False, reason, attempts, bad_credentials=True)

        if not portal_url:
            return LoginResult(False, "没有可用的认证页地址（portal_url 为空且未探测到跳转）", attempts)

        # ---- 取认证页 ----
        page, resp_desc = self._fetch_portal(portal_url)
        if page is None:
            return LoginResult(False, f"认证页打不开：{resp_desc}", attempts)

        if page.looks_logged_in(self.cfg.already_online_keywords):
            attempts += 1
            if verify():
                return LoginResult(True, "认证页显示已登录且网络已通", attempts, strategy="already")
            self.log.info("认证页显示已登录，但外网仍不通，继续尝试提交")

        # ---- 2. Dr.COM ----
        if self.cfg.drcom.enabled and looks_like_drcom(resp_desc or portal_url, page):
            self.log.info("识别为 Dr.COM / 哆点 ePortal 门户，使用专用策略")
            ctx = self.drcom.build_context(portal_url, page)
            ctx.account = username
            ctx.suffix = ctx.suffix or self.cfg.drcom.account_suffix
            self.log.info(
                "Dr.COM 上下文：host=%s port=%s 账号=%s IP=%s AC=%s/%s",
                ctx.host, ctx.port, _mask(ctx.final_account()), ctx.wlan_user_ip or "(未取到)",
                ctx.wlan_ac_ip or "-", ctx.wlan_ac_name or "-",
            )
            ok, msg, strategy, bad_cred = self.drcom.attempt(ctx, password, verify, preferred)
            attempts += self.drcom.attempts_used
            if ok:
                self._remember(strategy)
                return LoginResult(True, f"{strategy}：{msg}", attempts, strategy=strategy)
            if bad_cred:
                # 账号密码不对：换策略没有意义，直接失败（避免账号被锁）
                self.log.error("Dr.COM 认证失败：%s", msg)
                return LoginResult(False, msg, attempts, bad_credentials=True, strategy=strategy)

            # ---- AC 侧残留半死会话的恢复：先注销，再登录 ----
            # 只有"门户页明确是注销页(在线标记)"才算残留会话；单纯「页面超时」只是
            # 还没建立页面会话，不该触发注销。
            stale = page.drcom_page_state() == "online"
            if stale:
                self.log.info("疑似 AC 侧残留会话（认证页显示已登录 / 接口报页面超时），先注销再重试登录")
                done, logout_msg = self.drcom.logout(ctx)
                self.log.info("注销结果：%s", logout_msg)
                if done:
                    attempts += 1
                    time.sleep(1.0)
                    ok2, msg2, strategy2, bad2 = self.drcom.attempt(ctx, password, verify, preferred)
                    attempts += self.drcom.attempts_used
                    if ok2:
                        self._remember(strategy2)
                        return LoginResult(True, f"{strategy2}（注销旧会话后）：{msg2}",
                                           attempts, strategy=strategy2)
                    if bad2:
                        self.log.error("Dr.COM 认证失败：%s", msg2)
                        return LoginResult(False, msg2, attempts, bad_credentials=True, strategy=strategy2)
                    msg = f"注销旧会话后仍未成功：{msg2}"

            self.log.warning("Dr.COM 专用策略全部未生效：%s", msg)
            # 掉到通用表单兜底，不直接失败
            tail_reason = f"Dr.COM 策略未生效（{msg}）"

        else:
            tail_reason = ""

        # ---- 3. 通用 HTML 表单 ----
        form = page.pick_login_form()
        if form is None:
            dump = self._dump(page)
            extra = ""
            cands = page.script_candidates()
            if cands:
                extra = "；页面里疑似接口：" + ", ".join(cands[:5])
            reason = f"页面里找不到登录表单（标题：{page.title or '无'}）{extra}；已保存页面到 {dump}"
            if tail_reason:
                reason = tail_reason + "；" + reason
            return LoginResult(False, reason, attempts, dump_path=dump)

        user_f, pass_f = page.classify(form)
        if user_f is None or pass_f is None:
            dump = self._dump(page)
            return LoginResult(
                False,
                f"无法识别账号/密码输入框（字段：{form.field_names()}）；已保存页面到 {dump}",
                attempts,
                dump_path=dump,
            )

        if page.has_captcha(form):
            return LoginResult(
                False,
                "认证页含验证码，无法全自动登录：请联系网络中心关闭验证码，或用 custom 配置指定接口",
                attempts,
            )

        submit_url = page.action_url(form)
        self.log.info(
            "通用表单：%s %s，账号字段=%s，密码字段=%s",
            form.method.upper(), submit_url, user_f.name, pass_f.name,
        )

        variants = [
            ("form:标准提交", build_payload(
                form, username=username, password=password,
                username_field=user_f.name, password_field=pass_f.name,
                extra=self.cfg.custom.extra_fields if self.cfg.custom.enabled else None,
            ), form.method, False),
            ("form:带submit按钮", build_payload(
                form, username=username, password=password,
                username_field=user_f.name, password_field=pass_f.name,
                include_submit_button=True,
            ), form.method, False),
        ]
        if form.method == "get":
            variants.append(("form:GET查询串", variants[0][1], "get", True))

        last_reason = tail_reason
        bad_credentials = False
        for label, payload, method, force_get in variants:
            attempts += 1
            try:
                if force_get or method == "get":
                    resp2 = self.client.get_query(submit_url, payload, follow_redirects=True)
                else:
                    resp2 = self.client.post_form(submit_url, payload, follow_redirects=True)
            except Exception as exc:  # noqa: BLE001
                last_reason = f"{label}: 提交异常 {exc}"
                self.log.warning(last_reason)
                continue

            if resp2.status >= 400:
                last_reason = f"{label}: 服务端返回 HTTP {resp2.status}"
            else:
                last_reason = f"{label}: HTTP {resp2.status}"

            if any(str(k).lower() in resp2.text.lower() for k in self.cfg.bad_credential_keywords):
                bad_credentials = True
                last_reason = f"{label}: 服务端提示账号或密码错误"
                self.log.error("认证失败：%s", last_reason)
                break

            time.sleep(0.6)
            if verify():
                self._remember(label)
                return LoginResult(True, f"{label} 成功", attempts, strategy=label)
            self.log.info("%s 提交后外网仍不通，尝试下一个变体", label)

        if verify():
            return LoginResult(True, "认证后网络已通", attempts)

        dump = ""
        try:
            dump = self._dump(page)
        except Exception:  # noqa: BLE001
            pass
        return LoginResult(False, last_reason or "所有提交方式均未生效", attempts, bad_credentials, dump_path=dump)

    # ---------- 内部 ----------

    def _remember(self, strategy: str) -> None:
        if not strategy:
            return
        try:
            state = load_state()
            if state.get("last_strategy") != strategy:
                state["last_strategy"] = strategy
                state["last_success_ts"] = int(time.time())
                save_state(state)
                self.log.info("已记住可用策略：%s", strategy)
        except Exception:  # noqa: BLE001
            pass

    def _fetch_portal(self, portal_url: str) -> tuple[PortalPage | None, str]:
        try:
            resp = self.client.get(portal_url, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        page = PortalPage(resp.text, resp.url)
        return page, resp.url

    def _custom_login(self, username: str, password: str, verify: Verifier) -> tuple[bool, str, bool]:
        c = self.cfg.custom
        payload = dict(c.extra_fields or {})
        payload[c.username_field or "username"] = username
        payload[c.password_field or "password"] = password
        try:
            if c.method.upper() == "GET":
                resp = self.client.get_query(c.url, payload, encoding=c.encoding or None, follow_redirects=True)
            else:
                resp = self.client.post_form(c.url, payload, encoding=c.encoding or None, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"请求异常 {exc}", False
        time.sleep(0.6)
        if verify():
            return True, "ok", False
        if any(str(k).lower() in resp.text.lower() for k in self.cfg.bad_credential_keywords):
            return False, "账号或密码错误", True
        return False, f"提交后仍未联网（HTTP {resp.status}）", False

    def _dump(self, page: PortalPage) -> str:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = Path(LOG_DIR) / f"portal_dump_{int(time.time())}.html"
            path.write_text(f"<!-- url: {page.url} -->\n" + page.html, encoding="utf-8", errors="replace")
            return str(path)
        except Exception:  # noqa: BLE001
            return ""


def _mask(account: str) -> str:
    """日志里不打全账号。"""
    a = account or ""
    if len(a) <= 6:
        return a[:2] + "***"
    return a[:3] + "****" + a[-4:]


def describe(page: PortalPage) -> dict:
    return summarize(page)

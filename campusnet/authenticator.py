"""认证执行器：把账号密码送进校园网 Portal，并验证是否真的通了。

策略级联（每层都用**真实联网结果**判定成败，而不是只看返回文字）：
  1. custom 显式接口                 —— 用户手工指定的逃生舱
  2. Dr.COM / 哆点 ePortal 专用策略  —— 针对 Dr.COM 门户逆向实现，
                                        内部还有多套子策略逐个试
  3. 通用 HTML 表单自动识别          —— 覆盖绝大多数其他高校
成功的那套策略名会记进 state.json，下次优先复用。

V3.0 的安全改动（都集中在 :meth:`PortalAuthenticator.login` 的入口与出口）：

* 提交凭据之前一律过 :class:`campusnet.security.UrlPolicy`：目标主机必须是
  认证页本身、或配置里 ``trusted_auth_hosts`` 明确列出的地址，否则**停止提交**
  并回报 ``refused-target``。
* 返回值统一成 :class:`campusnet.adapters.AuthOutcome`（``LoginResult`` 是它的
  兼容别名），失败原因细分到"网络错 / 凭据错 / 需验证码 / 需人工 / 不支持"。
* 认证页转储默认关闭；打开时也只写**脱敏后**的副本，并自动清理旧转储。
* 日志里的账号统一走 :func:`campusnet.security.mask_account`。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import paths
from .adapters import (
    STATUS_BAD_CREDENTIALS,
    STATUS_CAPTCHA_REQUIRED,
    STATUS_MANUAL_REQUIRED,
    STATUS_NETWORK_ERROR,
    STATUS_REFUSED_TARGET,
    STATUS_SERVER_ERROR,
    STATUS_SUCCESS,
    STATUS_ALREADY_ONLINE,
    STATUS_UNSUPPORTED,
    STATUS_VERIFY_FAILED,
    AuthOutcome,
    adapter_plan,
)
from .config import Config, load_state, save_state
from .drcom import DrcomAdapter, looks_like_drcom
from .http import HttpClient
from .portal import PortalPage, build_payload, summarize
from .security import UrlPolicy, mask_account, redact_portal_html, redact_url

Verifier = Callable[[], bool]

# 兼容旧名字：界面与守护进程里一直用 LoginResult
LoginResult = AuthOutcome


class PortalAuthenticator:
    def __init__(self, client: HttpClient, cfg: Config, logger):
        self.client = client
        self.cfg = cfg
        self.log = logger
        self.drcom = DrcomAdapter(client, cfg, logger)
        self.policy = UrlPolicy(getattr(cfg, "trusted_auth_hosts", []) or [])
        self.last_plan: list = []

    # ---------- 对外入口 ----------

    def login(self, portal_url: str, username: str, password: str, verify: Verifier) -> AuthOutcome:
        attempts = 0
        state = load_state()
        preferred = str(state.get("last_strategy", "") or "")
        portal_url = (portal_url or "").strip()

        decision = self.policy.allow_portal(portal_url) if portal_url else None
        if decision is not None and not decision.allowed:
            return AuthOutcome(
                ok=False,
                status=STATUS_REFUSED_TARGET,
                reason=decision.reason,
                adapter="none",
                portal_url=portal_url,
            )

        # ---- 1. custom ----
        if self.cfg.custom.enabled and self.cfg.custom.url:
            target = self.policy.allow(self.cfg.custom.url, portal_url)
            if not target.allowed:
                self.log.error("自定义认证接口地址未通过校验：%s", target.reason)
                return AuthOutcome(
                    ok=False, status=STATUS_REFUSED_TARGET, reason=target.reason,
                    adapter="custom", portal_url=portal_url,
                )
            attempts += 1
            ok, reason, bad = self._custom_login(username, password, verify)
            if ok:
                self._remember("custom")
                return AuthOutcome(
                    ok=True, status=STATUS_SUCCESS, reason="custom 接口认证成功",
                    attempts=attempts, strategy="custom", adapter="custom", portal_url=portal_url,
                )
            self.log.warning("custom 接口认证失败：%s", reason)
            if bad:
                return AuthOutcome(
                    ok=False, status=STATUS_BAD_CREDENTIALS, reason=reason,
                    attempts=attempts, bad_credentials=True, adapter="custom", portal_url=portal_url,
                )

        if not portal_url:
            return AuthOutcome(
                ok=False,
                status=STATUS_NETWORK_ERROR,
                reason="没有可用的认证页地址（portal_url 为空且未探测到跳转）",
                attempts=attempts,
                adapter="none",
                retryable=True,
            )

        # ---- 取认证页 ----
        page, resp_desc = self._fetch_portal(portal_url)
        if page is None:
            return AuthOutcome(
                ok=False,
                status=STATUS_NETWORK_ERROR,
                reason=f"认证页打不开：{resp_desc}",
                attempts=attempts,
                adapter="none",
                portal_url=portal_url,
                retryable=True,
            )
        self.last_plan = adapter_plan(
            self.cfg, portal_url=resp_desc or portal_url, page=page, looks_like_drcom=looks_like_drcom
        )
        self.log.debug("候选认证方式：%s", "；".join(str(item) for item in self.last_plan))

        if page.looks_logged_in(self.cfg.already_online_keywords):
            attempts += 1
            if verify():
                return AuthOutcome(
                    ok=True, status=STATUS_ALREADY_ONLINE,
                    reason="认证页显示已登录且网络已通", attempts=attempts,
                    strategy="already", adapter="form", portal_url=portal_url,
                )
            self.log.info("认证页显示已登录，但外网仍不通，继续尝试提交")

        # ---- 2. Dr.COM ----
        if self.cfg.drcom.enabled and looks_like_drcom(resp_desc or portal_url, page):
            self.log.info("识别为 Dr.COM / 哆点 ePortal 门户，使用专用策略")
            ctx = self.drcom.build_context(portal_url, page)
            ctx.account = username
            ctx.suffix = ctx.suffix or self.cfg.drcom.account_suffix
            self.log.info(
                "Dr.COM 上下文：host=%s port=%s 账号=%s IP=%s AC=%s/%s",
                ctx.host, ctx.port, mask_account(ctx.final_account()), ctx.wlan_user_ip or "(未取到)",
                ctx.wlan_ac_ip or "-", ctx.wlan_ac_name or "-",
            )
            ok, msg, strategy, bad_cred = self.drcom.attempt(ctx, password, verify, preferred)
            attempts += self.drcom.attempts_used
            if ok:
                self._remember(strategy)
                return AuthOutcome(
                    ok=True, status=STATUS_SUCCESS, reason=f"{strategy}：{msg}",
                    attempts=attempts, strategy=strategy, adapter="drcom", portal_url=portal_url,
                )
            if bad_cred:
                # 账号密码不对：换策略没有意义，直接失败（避免账号被锁）
                self.log.error("Dr.COM 认证失败：%s", msg)
                return AuthOutcome(
                    ok=False, status=STATUS_BAD_CREDENTIALS, reason=msg,
                    attempts=attempts, bad_credentials=True,
                    strategy=strategy, adapter="drcom", portal_url=portal_url,
                )

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
                        return AuthOutcome(
                            ok=True, status=STATUS_SUCCESS,
                            reason=f"{strategy2}（注销旧会话后）：{msg2}",
                            attempts=attempts, strategy=strategy2, adapter="drcom",
                            portal_url=portal_url,
                        )
                    if bad2:
                        self.log.error("Dr.COM 认证失败：%s", msg2)
                        return AuthOutcome(
                            ok=False, status=STATUS_BAD_CREDENTIALS, reason=msg2,
                            attempts=attempts, bad_credentials=True,
                            strategy=strategy2, adapter="drcom", portal_url=portal_url,
                        )
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
                extra = "；页面里疑似接口：" + ", ".join(redact_url(u) for u in cands[:5])
            reason = f"页面里找不到登录表单（标题：{page.title or '无'}）{extra}"
            if dump:
                reason += f"；已保存脱敏页面到 {dump}"
            if tail_reason:
                reason = tail_reason + "；" + reason
            return AuthOutcome(
                ok=False, status=STATUS_UNSUPPORTED, reason=reason, attempts=attempts,
                needs_user=True, retryable=False, adapter="none",
                portal_url=portal_url, dump_path=dump,
            )

        user_f, pass_f = page.classify(form)
        if user_f is None or pass_f is None:
            dump = self._dump(page)
            reason = f"无法识别账号/密码输入框（字段：{form.field_names()}）"
            if dump:
                reason += f"；已保存脱敏页面到 {dump}"
            return AuthOutcome(
                ok=False, status=STATUS_UNSUPPORTED, reason=reason, attempts=attempts,
                needs_user=True, retryable=False, adapter="form",
                portal_url=portal_url, dump_path=dump,
            )

        if page.has_captcha(form):
            return AuthOutcome(
                ok=False,
                status=STATUS_CAPTCHA_REQUIRED,
                reason="认证页含验证码，无法全自动登录：请用浏览器模式手动完成，"
                       "或联系网络中心关闭验证码 / 用 custom 配置指定接口",
                attempts=attempts,
                needs_user=True,
                retryable=False,
                adapter="form",
                portal_url=portal_url,
            )

        submit_url = page.action_url(form)
        target = self.policy.allow(submit_url, page.url, portal_url)
        if not target.allowed:
            # 表单 action 指向了没确认过的主机：绝不提交账号密码
            self.log.error("表单提交地址未通过校验：%s", target.reason)
            return AuthOutcome(
                ok=False, status=STATUS_REFUSED_TARGET, reason=target.reason,
                attempts=attempts, needs_user=True, retryable=False,
                adapter="form", portal_url=portal_url,
            )

        self.log.info(
            "通用表单：%s %s，账号字段=%s，密码字段=%s",
            form.method.upper(), redact_url(submit_url), user_f.name, pass_f.name,
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
        network_error = False
        for label, payload, method, force_get in variants:
            attempts += 1
            try:
                if force_get or method == "get":
                    resp2 = self.client.get_query(submit_url, payload, follow_redirects=True)
                else:
                    resp2 = self.client.post_form(submit_url, payload, follow_redirects=True)
            except Exception as exc:  # noqa: BLE001
                network_error = True
                last_reason = f"{label}: 提交异常 {exc}"
                self.log.warning(last_reason)
                continue

            if resp2.status >= 400:
                last_reason = f"{label}: 服务端返回 HTTP {resp2.status}"
            else:
                last_reason = f"{label}: HTTP {resp2.status}"

            if any(str(k).lower() in resp2.text.lower() for k in self.cfg.bad_credential_keywords):
                last_reason = f"{label}: 服务端提示账号或密码错误"
                self.log.error("认证失败：%s", last_reason)
                return AuthOutcome(
                    ok=False, status=STATUS_BAD_CREDENTIALS, reason=last_reason,
                    attempts=attempts, bad_credentials=True, strategy=label,
                    adapter="form", portal_url=portal_url,
                )

            time.sleep(0.6)
            if verify():
                self._remember(label)
                return AuthOutcome(
                    ok=True, status=STATUS_SUCCESS, reason=f"{label} 成功",
                    attempts=attempts, strategy=label, adapter="form", portal_url=portal_url,
                )
            self.log.info("%s 提交后外网仍不通，尝试下一个变体", label)

        if verify():
            return AuthOutcome(
                ok=True, status=STATUS_SUCCESS, reason="认证后网络已通",
                attempts=attempts, strategy="form:unknown", adapter="form", portal_url=portal_url,
            )

        dump = ""
        try:
            dump = self._dump(page)
        except Exception:  # noqa: BLE001
            pass
        if network_error:
            status = STATUS_NETWORK_ERROR
            retryable = True
        elif tail_reason or last_reason:
            status = STATUS_VERIFY_FAILED if "HTTP 2" in (last_reason or "") else STATUS_SERVER_ERROR
            retryable = True
        else:
            status = STATUS_SERVER_ERROR
            retryable = True
        return AuthOutcome(
            ok=False, status=status, reason=last_reason or "所有提交方式均未生效",
            attempts=attempts, adapter="form", portal_url=portal_url,
            dump_path=dump, retryable=retryable,
        )

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
        """按配置决定是否保存**脱敏后**的认证页副本（默认关闭）。"""
        if not getattr(self.cfg, "portal_dump_enabled", False):
            return ""
        try:
            directory = paths.log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = Path(directory) / f"portal_dump_{int(time.time())}.html"
            path.write_text(redact_portal_html(page.html, page.url), encoding="utf-8", errors="replace")
            self._prune_dumps(directory)
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    def _prune_dumps(self, directory: Path) -> None:
        limit = int(getattr(self.cfg, "portal_dump_max_files", 5) or 0)
        if limit <= 0:
            return
        try:
            files = sorted(directory.glob("portal_dump_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in files[limit:]:
                try:
                    stale.unlink()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


def describe(page: PortalPage) -> dict:
    return summarize(page)

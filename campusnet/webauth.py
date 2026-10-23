"""用真浏览器完成网页认证：开窗口 → **先验证来源** → 注入脚本填写 → 复核真实联网。

这是参考项目 CampusConnect 的核心做法（WebView2 里跑注入 JS），我们换成
系统自带 Edge/Chrome + CDP 实现，见 :mod:`campusnet.browser` 与
:mod:`campusnet.portal_js`。

和"直接 HTTP 提交"（:mod:`campusnet.authenticator`）的分工：

* HTTP 提交：不依赖浏览器，快，适合普通表单和已知接口（Dr.COM 也是这样打的）；
* 浏览器提交：能应付 JS 渲染、加密参数、协议勾选、运营商下拉这些页面行为。

两条路都会用**真实联网**结果来判定成功，不会只信页面文字。

V3.0 的安全改动 —— **两阶段**填写：

1. **探针阶段**：先只执行一个不含任何凭据的脚本，读回 ``location``、``origin``、
   ``protocol``、``readyState``。只有当页面确实是 http/https 网页、且 origin 与
   已保存的认证页完全一致时，才进入下一阶段。
2. **填写阶段**：此时才把账号密码拼进注入脚本。

这样"重定向到另一个网站"、"跳到应用内页面"、"证书错误页"这三种情况都**不会**
让密码离开本进程。遇到这些情况一律停手，把窗口留给用户手动处理。
"""

from __future__ import annotations

import threading
import time

from . import browser, portal_js, paths
from .adapters import (
    STATUS_CAPTCHA_REQUIRED,
    STATUS_MANUAL_REQUIRED,
    STATUS_NETWORK_ERROR,
    STATUS_REFUSED_TARGET,
    STATUS_SERVER_ERROR,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    AuthOutcome,
)
from .checker import check
from .config import Config
from .daemon import build_client
from .security import origin_of, redact_url

# 模块级只允许一个浏览器会话：界面上"停止"要能把它关掉
_active: browser.BrowserSession | None = None
_lock = threading.Lock()


def close_active() -> None:
    """关掉当前留下的浏览器窗口（认得就关，不认得就算了）。"""
    global _active
    with _lock:
        session, _active = _active, None
    if session is not None:
        session.close()


def active_open() -> bool:
    return _active is not None


def _result(outcome_status: str, reason: str, *, state: str = "", needs_user: bool = False,
            kept_open: bool = False, ok: bool = False) -> dict:
    """统一返回结构（GUI/守护进程/测试都读这一份）。"""
    return {
        "ok": ok,
        "status": outcome_status,
        "reason": reason,
        "state": state,
        "needs_user": needs_user,
        "kept_open": kept_open,
    }


def run(cfg: Config, password: str, *, progress=None, stop: threading.Event | None = None,
        portal_url: str = "", timeout: float = 120.0) -> dict:
    """打开认证页、验证来源、填写并提交。返回 :func:`_result` 结构。"""

    def say(text: str) -> None:
        if progress is not None:
            try:
                progress(text)
            except Exception:  # noqa: BLE001
                pass

    def stopped() -> bool:
        return bool(stop is not None and stop.is_set())

    global _active
    client = build_client(cfg)

    # ---- 1. 决定要打开哪个地址 ----
    portal = (portal_url or cfg.portal_url or "").strip()
    if not portal:
        say("未配置认证页面地址，正在探测…")
        try:
            state = check(client, cfg)
            portal = state.portal_url
        except Exception as exc:  # noqa: BLE001
            return _result(STATUS_NETWORK_ERROR, f"探测失败：{exc}", needs_user=True)
    if not portal:
        return _result(
            STATUS_NETWORK_ERROR,
            "未找到认证页面地址。请点击「探测认证地址」，或在「设置」中填写。",
            needs_user=True,
        )

    expected_origin = origin_of(portal)
    if not expected_origin:
        return _result(
            STATUS_REFUSED_TARGET,
            f"认证页地址不合法（{redact_url(portal)}），已停止自动填写。",
            needs_user=True,
        )

    # ---- 2. 打开浏览器 ----
    say(f"正在打开认证页面：{redact_url(portal)}")
    try:
        session = browser.open_portal(portal)
    except browser.BrowserError as exc:
        return _result(STATUS_NETWORK_ERROR, str(exc), needs_user=True)
    previous = None
    with _lock:
        previous, _active = _active, session
    if previous is not None and previous is not session:
        previous.close()  # 旧窗口先关掉，别同时开两个

    deadline = time.time() + max(20.0, float(timeout))
    probe = portal_js.build_probe(expected_origin)

    # ---- 3. 探针阶段：确认来源，再放凭据 ----
    source_ok = False
    last_probe_state = "waiting-ready"
    while time.time() < deadline:
        if stopped():
            return _result(STATUS_MANUAL_REQUIRED, "已停止。", state=last_probe_state,
                           needs_user=True, kept_open=True)
        try:
            info = session.devtools.evaluate(probe)
        except browser.BrowserError:
            time.sleep(0.5)
            continue
        if not isinstance(info, dict):
            time.sleep(0.5)
            continue
        state = str(info.get("state") or "waiting-ready")
        last_probe_state = state
        if state == "ready":
            source_ok = True
            break
        if state == "wrong-origin":
            say(f"页面跳到了 {redact_url(info.get('href') or '')}，与已保存的认证页不同")
            return _result(
                STATUS_REFUSED_TARGET,
                "认证页跳转到了另一个地址，为避免把账号密码填到未确认的页面，已停止自动填写。"
                "如果这是学校新的认证地址，请在设置里更新认证页地址后重试。",
                state=state, needs_user=True, kept_open=True,
            )
        if state == "page-error":
            return _result(
                STATUS_NETWORK_ERROR,
                "认证页面加载失败。常见原因：门户证书不受信任（HTTPS 自签证书）或网络中断。"
                "请在该窗口里手动打开一次认证页并继续，或在设置里把门户地址加入 TLS 放行列表。",
                state=state, needs_user=True, kept_open=True,
            )
        if state == "insecure-origin":
            return _result(
                STATUS_REFUSED_TARGET,
                "当前页面不是普通网页（可能被重定向到本地文件或浏览器内部页面），已停止自动填写。",
                state=state, needs_user=True, kept_open=True,
            )
        time.sleep(0.4)

    if not source_ok:
        return _result(
            STATUS_MANUAL_REQUIRED,
            "认证页面在规定时间内没有加载完成，已停止自动填写。",
            state=last_probe_state, needs_user=True, kept_open=True,
        )

    # ---- 4. 填写阶段：来源已确认，现在才把账号密码拼进脚本 ----
    say("来源已确认，开始填写账号密码。")
    script_cfg = {
        "origin": expected_origin,
        "username": cfg.username,
        "password": password,
        "submit": bool(cfg.autofill_click),
        "service": (getattr(cfg, "service_option", "") or "").strip(),
        "userSelector": (getattr(cfg, "user_selector", "") or "").strip(),
        "passwordSelector": (getattr(cfg, "password_selector", "") or "").strip(),
        "submitSelector": (getattr(cfg, "submit_selector", "") or "").strip(),
        "statusOnly": False,
    }
    script = portal_js.build(script_cfg)

    last_state = ""
    needs_user = False
    reason = ""
    online = False
    outcome_status = STATUS_SERVER_ERROR
    next_online_check = 0.0
    wrong_origin_hits = 0

    try:
        while time.time() < deadline:
            if stopped():
                return _result(STATUS_MANUAL_REQUIRED, "已停止。", state=last_state,
                               needs_user=True, kept_open=True)

            try:
                state = session.devtools.evaluate(script)
            except browser.BrowserError as exc:
                # 页面跳走后上一条执行上下文会失效，稍等重试即可
                last_state = last_state or "waiting-ready"
                say(f"页面跳转中（{exc}）")
                time.sleep(0.6)
                continue

            state = str(state or "")
            if state != last_state:
                last_state = state
                say(portal_js.describe(state))

            if state == "authenticated":
                reason = "认证页面显示登录成功。"
                outcome_status = STATUS_SUCCESS
                break
            if state == "authentication-rejected":
                reason = "认证页面显示登录失败。"
                outcome_status = STATUS_SERVER_ERROR
                break
            if state == "wrong-origin":
                # 门户有时会跳到另一个域名的认证页（10.0.0.1 -> auth.xxx.edu.cn）。
                # 给它几秒确认是"真的换了地址"再停，避免一闪而过就误判。
                wrong_origin_hits += 1
                if wrong_origin_hits >= 8:
                    reason = "页面地址与已保存的认证页面地址不一致，已停止填写。"
                    needs_user = True
                    outcome_status = STATUS_REFUSED_TARGET
                    break
                time.sleep(0.4)
                continue
            wrong_origin_hits = 0
            if state == "manual-required":
                reason = portal_js.describe(state)
                needs_user = True
                outcome_status = STATUS_CAPTCHA_REQUIRED
                break
            if state in ("agreement-required", "missing-service",
                         "service-required", "submission-unconfirmed", "invalid-selector",
                         "insecure-origin", "page-error"):
                reason = portal_js.describe(state)
                needs_user = True
                outcome_status = STATUS_MANUAL_REQUIRED
                break
            if state == "filled" and not script_cfg["submit"]:
                reason = "账号与密码已填写，请手动点击登录。"
                needs_user = True
                outcome_status = STATUS_MANUAL_REQUIRED
                break
            if state == "missing-submit" and time.time() > deadline - timeout * 0.4:
                reason = "已填写账号与密码，但未找到登录按钮，请手动点击。"
                needs_user = True
                outcome_status = STATUS_MANUAL_REQUIRED
                break

            # 提交之后（或页面已登录）都用真实联网复核，别只信页面文字
            now = time.time()
            if now >= next_online_check:
                next_online_check = now + 1.5
                try:
                    if check(client, cfg).online:
                        online = True
                        reason = reason or "网络可用。"
                        outcome_status = STATUS_SUCCESS
                        break
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001  浏览器中途崩了也不能把界面搞挂
        reason = reason or f"浏览器交互出错：{exc}"
        needs_user = True
        outcome_status = STATUS_SERVER_ERROR

    # ---- 5. 收尾：通了就关窗口；需要人工就把窗口留着 ----
    keep = needs_user and not online
    if keep:
        say("窗口保留。在页面中完成剩余步骤后，点击「检查上网状态」。")
    else:
        with _lock:
            if _active is session:
                _active = None
        session.close()

    if online or last_state in ("authenticated", "submitted", "awaiting-authentication"):
        ok = online
        if not ok:
            # 页面说提交/成功，但外网还没通：再复核一次，避免"服务端回了成功其实没网"
            try:
                ok = check(client, cfg).online
            except Exception:  # noqa: BLE001
                ok = False
        return _result(
            STATUS_SUCCESS if ok else STATUS_SERVER_ERROR,
            reason or ("网络可用。" if ok else "已提交，但网络仍不可用。"),
            state=last_state, needs_user=not ok, kept_open=keep, ok=ok,
        )

    return _result(
        outcome_status,
        reason or portal_js.describe(last_state) or "认证未完成。",
        state=last_state, needs_user=needs_user, kept_open=keep,
    )


# 供 GUI 显示"浏览器模式会把临时文件放哪"
def profile_location() -> str:
    return str(paths.browser_profile_root())


def to_outcome(result: dict) -> AuthOutcome:
    """把浏览器模式的 dict 结果转成统一结构，便于守护进程复用同一套判定。"""
    result = result or {}
    return AuthOutcome(
        ok=bool(result.get("ok")),
        status=str(result.get("status") or STATUS_SERVER_ERROR),
        reason=str(result.get("reason") or ""),
        strategy="browser",
        adapter="browser",
        needs_user=bool(result.get("needs_user")),
        details={"state": result.get("state", ""), "kept_open": result.get("kept_open", False)},
    )

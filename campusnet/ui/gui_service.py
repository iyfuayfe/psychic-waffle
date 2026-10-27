"""界面背后的动作层。

界面只负责画和显示，**所有**网络请求、netsh 调用都在后台线程里通过这些函数完成：
界面线程一旦被阻塞，窗口就会"未响应"，这是 tkinter 最常见的卡死原因。

约定：

* 每个函数要么返回结果，要么把失败原因装在返回值里，**绝不抛异常**（编程错误除外）；
* 长动作用 ``progress`` 回调把"正在做什么"吐给界面；
* 可中断的动作接受 ``stop``（threading.Event），在步骤之间检查；
* 任何返回值里都不含明文密码。
"""

from __future__ import annotations

import threading
import time

from .. import autostart, crypto, diagnostics, paths, status, wifi
from ..adapters import AuthOutcome
from ..authenticator import PortalAuthenticator
from ..checker import STATE_HIJACK, STATE_REDIRECT, check
from ..config import Config, ConfigError, LoadReport
from ..daemon import build_client, load_heartbeat, make_verifier
from ..drcom import looks_like_drcom
from ..logs import get_logger as _fallback_logger, setup_logging
from ..portal import PortalPage, portal_page_evidence
from ..security import redact_text, redact_url

_logger = None
_logger_lock = threading.Lock()


def get_logger():
    """GUI 期间复用同一个 logger。

    **失败时绝不能递归调用自身**：``_logger_lock`` 是不可重入的
    :class:`threading.Lock`，一旦初始化抛异常（日志目录不可写、数据目录被策略
    限制、配置解析失败等），原来那句 ``_logger = get_logger()`` 会在同一线程
    再次抢同一把锁，导致**永久死锁**——界面从此点任何按钮都没反应，而且因为
    日志没建起来，用户拿不到任何线索。

    这里改为退回 ``campusnet.logs.get_logger()``（它保证返回可用 logger，
    没有 handler 时会挂 ``NullHandler``），界面始终可用。
    另外加了无锁快路径，避免每次调用都去抢锁。
    """
    global _logger
    if _logger is not None:
        return _logger
    with _logger_lock:
        if _logger is None:
            try:
                _logger = setup_logging(load_config(), console=False)
            except Exception:  # noqa: BLE001
                _logger = _fallback_logger()
        return _logger


# ------------------------------------------------ 配置

# 配置是"读-改-写"的：保存设置、把扫到的 SSID 设为目标、记住探测到的门户地址
# 都会经过这里。多个后台线程同时做 RMW 会互相覆盖，所以用一把模块级锁串起来。
_config_lock = threading.Lock()

# 记住最近一次配置读取失败的原因（供界面显示"配置读取失败"，而不是静默用默认值）
_last_config_error: str = ""


def load_config() -> Config:
    """读配置。

    读失败时返回默认配置**但会记住原因**（:func:`last_config_error`），界面据此
    显示"配置读取失败"，而不是把默认值当成用户配置。原文件不会被覆盖。
    """
    global _last_config_error
    try:
        cfg = Config.load(paths.config_path())
    except ConfigError as exc:
        _last_config_error = str(exc)
        return Config()
    _last_config_error = ""
    return cfg


def last_config_error() -> str:
    """最近一次读配置失败的原因（空字符串表示正常）。"""
    return _last_config_error


def load_config_report() -> tuple[Config, LoadReport]:
    report = LoadReport()
    try:
        cfg = Config.load(paths.config_path(), report=report)
    except ConfigError as exc:
        report.problems = [("config", str(exc))]
        cfg = Config()
    return cfg, report


def config_error() -> str:
    """配置文件是否有语法错误（有则返回给用户看的说明）。"""
    try:
        Config.load(paths.config_path())
        return ""
    except ConfigError as exc:
        return str(exc)


def config_write_problem() -> str:
    """判断"磁盘上的配置现在能不能被安全读回"（空串表示可以写）。

    为什么不只看 ``last_config_error()``：那是模块级缓存，只反映"最近一次读取"的
    结果，长跑进程或测试里会被别的调用污染。写盘前重新读一次最可靠；首次运行
    （文件还不存在）必须放行 —— :meth:`Config.load` 对不存在的文件返回默认值。
    """
    try:
        Config.load(paths.config_path())
    except ConfigError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"读取配置时出错：{exc}"
    return ""


def save_config(cfg: Config) -> tuple[bool, str]:
    """保存配置到用户数据目录（该目录默认只有当前用户可访问）。

    **读不回来时拒绝写入**：调用方（概览页「设为目标」、认证页记住门户地址等）
    手里往往是一份"读失败后回退出来的默认配置"，直接写盘会把用户原始配置覆盖成
    默认值（账号、密码、门户地址全丢）。典型场景：配置由更高版本写入，本版读不了
    ——这时宁可报错，也不能覆盖。
    """
    problem = config_write_problem()
    if problem:
        return False, f"配置当前无法安全写回（{problem}），为避免覆盖你的原始配置，本次没有写入。"
    try:
        with _config_lock:
            cfg.save(paths.config_path())
    except Exception as exc:  # noqa: BLE001
        return False, f"保存失败：{exc}"
    return True, "已保存"


def update_config(mutate) -> tuple[bool, str, Config | None]:
    """安全的"读-改-写"：在锁内重新读盘、应用改动、再写回。

    ``mutate(cfg)`` 负责就地修改配置。**读失败时不会继续写**，避免把用户原始
    配置（可能是手工编辑过的）用一份默认值覆盖掉 —— 这是数据完整性问题。

    返回 ``(成功, 消息, 新配置)``。
    """
    global _last_config_error
    with _config_lock:
        try:
            cfg = Config.load(paths.config_path())
        except ConfigError as exc:
            _last_config_error = str(exc)
            return False, (
                f"配置文件无法解析，为避免覆盖你的原始配置，本次没有写入：{exc}"
            ), None
        try:
            mutate(cfg)
            cfg.validate()
            cfg.save(paths.config_path())
        except Exception as exc:  # noqa: BLE001
            return False, f"保存失败：{exc}", None
        _last_config_error = ""
        return True, "已保存", cfg


def set_password(cfg: Config, password: str) -> tuple[bool, str]:
    """把明文密码加密进 cfg.password_enc（DPAPI，和本机 + 当前用户绑定）。"""
    if not password:
        return False, "密码为空"
    started = time.perf_counter()
    try:
        cfg.password_enc = crypto.protect(password)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"加密失败：{exc}"
    finally:
        # DPAPI 在域账户 / 挂杀软钩子时可能明显变慢，记下耗时便于排障
        try:
            get_logger().debug("DPAPI 加密耗时 %.0f ms", (time.perf_counter() - started) * 1000)
        except Exception:  # noqa: BLE001
            pass


def has_password(cfg: Config) -> bool:
    """配置里是否有一个**本机解得开**的密码。

    注意：这一步要走 DPAPI 解密，**不要在 UI 线程里对用户点击做同步调用**
    （解不开、或系统 DPAPI 变慢时会让窗口卡住）。界面上请用
    :func:`has_saved_password` 做快速判断，把本函数放到后台线程。
    """
    if not cfg.password_enc:
        return False
    try:
        crypto.unprotect(cfg.password_enc)
        return True
    except Exception:  # noqa: BLE001
        return False


def has_saved_password(cfg: Config) -> bool:
    """快速判断"存过密码没有"——只看密文是否非空，**不做 DPAPI 解密**。

    给界面用：几百微秒级，可以放心在 UI 线程调用。
    至于"这台机器到底能不能解开"，交给后台线程用 :func:`has_password` 验证。
    """
    return bool(getattr(cfg, "password_enc", ""))


def password_status() -> dict:
    """后台用：判断已保存的密码在本机能不能解开（会走 DPAPI 解密）。

    界面在打开「设置」页时用它在后台复查一次，避免把"存着一份解不开的密文"
    说成"已保存密码"（换过 Windows 用户 / 换过电脑的典型情况）。
    """
    cfg = load_config()
    if not has_saved_password(cfg):
        return {"has": False, "ok": False}
    return {"has": True, "ok": has_password(cfg)}


def save_settings(values: dict, password: str, *, enable: bool) -> dict:
    """保存设置（**供后台线程调用**，不要在 UI 线程里直接跑）。

    把三件可能很慢的事都放在这里：DPAPI 加密、写文件、回读校验。
    返回结构::

        {"ok": bool, "message": str, "config": Config|None,
         "password_ok": bool, "enable": bool, "timings": {...}}

    所有耗时都记进 ``timings``（毫秒），出问题时能一眼看出卡在哪一步。
    """
    timings: dict = {}
    started = time.perf_counter()

    def mark(name: str, since: float) -> float:
        timings[name] = round((time.perf_counter() - since) * 1000, 1)
        return time.perf_counter()

    try:
        # 1) 读当前配置（保留用户没在界面上暴露的字段）
        #    注意：**读失败就中止**，绝不用一份默认配置覆盖用户原文件
        stamp = time.perf_counter()
        try:
            cfg = Config.load(paths.config_path())
        except ConfigError as exc:
            return {
                "ok": False,
                "message": (
                    "配置文件无法解析，为避免覆盖你的原始配置，本次没有写入。"
                    f"原因：{exc}。可先备份后删除该文件再保存。"
                ),
                "config": None,
                "password_ok": False,
                "enable": enable,
                "timings": timings,
            }
        stamp = mark("load_ms", stamp)

        # 2) 套用界面上的值
        cfg.wifi_ssid = values.get("wifi_ssid", cfg.wifi_ssid)
        cfg.wifi_connect_mode = values.get("wifi_connect_mode", cfg.wifi_connect_mode)
        cfg.portal_auth_enabled = bool(values.get("portal_auth_enabled", True))
        cfg.browser_fallback = bool(values.get("browser_fallback", False))
        cfg.portal_url = values.get("portal_url", cfg.portal_url)
        cfg.username = values.get("username", cfg.username)
        cfg.service_option = values.get("service_option", cfg.service_option)
        cfg.autofill_click = bool(values.get("autofill_click", True))
        cfg.user_selector = values.get("user_selector", "")
        cfg.password_selector = values.get("password_selector", "")
        cfg.submit_selector = values.get("submit_selector", "")
        custom_url = values.get("custom_url", "")
        cfg.custom.url = custom_url
        cfg.custom.username_field = values.get("custom_user", "")
        cfg.custom.password_field = values.get("custom_password", "")
        cfg.custom.enabled = bool(custom_url)
        if enable or values.get("auto_enabled"):
            cfg.auto_enabled = True

        # 3) 加密密码（慢点可能在这里）
        if password:
            ok, message = set_password(cfg, password)
            stamp = mark("encrypt_ms", stamp)
            if not ok:
                return {"ok": False, "message": message, "config": None,
                        "password_ok": False, "enable": enable, "timings": timings}
        else:
            stamp = mark("encrypt_ms", stamp)

        # 4) 规范化 + 原子写盘（实时防护可能让写盘变慢）
        #    加锁：与「设为目标 SSID」「记住门户地址」等其它读-改-写串行化，
        #    否则两个后台线程同时 RMW 会互相覆盖对方刚写的字段。
        try:
            with _config_lock:
                cfg.validate()
                cfg.save(paths.config_path())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"保存失败：{exc}", "config": None,
                    "password_ok": False, "enable": enable, "timings": timings}
        stamp = mark("write_ms", stamp)

        # 5) 回读校验：能不能真的解出密码（也在这里做，别回 UI 线程）
        password_ok = True
        if cfg.password_enc:
            password_ok = has_password(cfg)
        mark("verify_ms", stamp)
        mark("total_ms", started)

        return {
            "ok": True,
            "message": "已保存",
            "config": cfg,
            "password_ok": password_ok,
            "enable": enable,
            "timings": timings,
        }
    except Exception as exc:  # noqa: BLE001
        mark("total_ms", started)
        return {"ok": False, "message": f"保存失败：{exc}", "config": None,
                "password_ok": False, "enable": enable, "timings": timings}


# ------------------------------------------------ 无线网络


def wifi_snapshot() -> dict:
    st = wifi.interface_status()
    return {
        "available": st.available,
        "connected": st.connected,
        "ssid": st.ssid,
        "signal": st.signal,
        "state": st.state,
        "description": st.description,
        "error": st.error,
        "label": st.describe(),
    }


def scan_networks() -> tuple[list[dict], str]:
    """扫描附近网络。返回 (网络列表, 错误说明)。

    错误说明是**给用户看的**：如果是 Windows 位置权限把 WLAN 信息挡住了，
    这里会直接告诉他去哪儿打开开关，而不是留一句"扫描失败"。
    """
    try:
        nets = wifi.scan()
    except wifi.WifiError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    return (
        [
            {
                "ssid": n.ssid,
                "signal": n.signal,
                "encrypted": n.encrypted,
                "kind": n.kind_text,
                "label": n.signal_text,
            }
            for n in nets
        ],
        "",
    )


def connect_wifi(ssid: str, mode: str = "open") -> tuple[bool, str]:
    return wifi.connect(ssid, mode)


def open_wifi_settings() -> bool:
    return wifi.open_wifi_settings()


# ------------------------------------------------ 联网状态与认证


def online_status() -> dict:
    """探测能不能上外网。返回 {online, kind, detail, portal_url, hint}。"""
    cfg = load_config()
    client = build_client(cfg)
    try:
        state = check(client, cfg)
    except Exception as exc:  # noqa: BLE001
        return {"online": False, "kind": "error", "detail": str(exc),
                "portal_url": "", "hint": "探测过程本身出错，详情见日志。"}
    return {
        "online": state.online,
        "kind": state.kind,
        "detail": state.detail,
        "portal_url": state.portal_url,
        "hint": state.explain(),
    }


def _probe_result(state: str, *, kind: str = "", detail: str = "", advice: str = "",
                  url: str = "", http_status: int = 0, title: str = "",
                  is_drcom: bool = False, form_count: int = 0,
                  evidence=None, persistable: bool = False, from_redirect: bool = False,
                  needs_confirm: bool = False, manual_entry: bool = False,
                  candidate_url: str = "") -> dict:
    """组装 :func:`probe_portal` 的返回值。

    ``label`` / ``severity`` / ``advice`` 一律由 :mod:`campusnet.status` 给出，
    界面不再自己拼状态文字（这是 status.py 模块注释里定下的纪律）。
    """
    portal_type = ""
    if state == status.PROBE_PORTAL and url:
        portal_type = "城市热点 Dr.COM / 哆点 ePortal" if is_drcom else "通用网页认证"
    data = {
        # 旧字段：ok 仍然只表示"确实发现了认证门户"，语义不变
        "ok": state == status.PROBE_PORTAL,
        "state": state,
        "kind": kind,
        "detail": detail,
        "advice": advice,
        "url": url,
        "portal_url": url,
        "http_status": int(http_status or 0),
        "title": title,
        "is_drcom": bool(is_drcom),
        "form_count": int(form_count or 0),
        "portal_type": portal_type,
        "evidence": list(evidence or []),
        "persistable": bool(persistable),
        "from_redirect": bool(from_redirect),
        "needs_confirm": bool(needs_confirm),
        "manual_entry": bool(manual_entry),
        "candidate_url": candidate_url,
    }
    state_obj = status.from_probe(data)
    data["label"] = state_obj.label
    data["severity"] = state_obj.severity
    if not advice:
        data["advice"] = state_obj.advice
    return data


def probe_portal(url: str = "") -> dict:
    """探测认证页：先看有没有跳转，再看页面有没有门户证据。

    三种结论（``state``），对应"不要把联网检测和门户发现混为一谈"的要求：

    * ``online`` —— 已确认能正常访问互联网：**当前不需要认证门户**。这是正常业务
      结果，不是失败。3.1.0 把它和"探测失败"压成同一个 ``ok=False``，界面于是对着
      一个能正常上网的用户报「探测失败 / 未探测到认证页面」——用户实际反馈的现象。
    * ``portal-suspected`` —— 有明确证据（网关跳转 + 页面门户特征）表明请求被认证
      门户拦截；``url`` 给出门户地址。
    * ``unknown`` —— 探测失败、证据不足或结果矛盾：**只说"无法确定"**，不编造门户
      地址，也不把普通网页 / 错误页 / 代理错误页当成认证门户。
    """
    cfg = load_config()
    client = build_client(cfg)
    target = (url or "").strip() or cfg.portal_url
    from_redirect = False
    kind = ""

    if not target:
        try:
            net = check(client, cfg)
        except Exception as exc:  # noqa: BLE001
            return _probe_result(
                status.PROBE_UNKNOWN,
                detail=f"联网探测失败：{redact_text(str(exc))}",
                advice="稍后重试；若一直失败，请点「查看日志」把记录发给维护者",
                manual_entry=True,
            )
        kind = net.kind
        if net.online:
            # ★ 关键修复点：网络已确认可用 = ONLINE（不需要认证门户），不是"探测失败"
            return _probe_result(
                status.PROBE_ONLINE,
                kind=net.kind,
                detail="已确认能正常访问互联网，当前没有认证门户。",
            )
        target = net.portal_url
        from_redirect = net.kind == STATE_REDIRECT
        if not target:
            return _probe_result(
                status.PROBE_UNKNOWN,
                kind=net.kind,
                detail=net.explain(),
                advice="确认已连接校园 Wi-Fi 后重试；若已连上仍探测不到，"
                       "请在「设置」里手工填写认证页地址（校园网先拦 DNS 时会影响自动探测）",
                manual_entry=True,
            )

    try:
        resp = client.get(target, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        return _probe_result(
            status.PROBE_UNKNOWN,
            kind=kind,
            detail=f"候选认证页打不开：{redact_text(str(exc))}",
            advice="确认已连接校园 Wi-Fi；也可以手工填写认证页地址",
            manual_entry=True,
            candidate_url=redact_url(target),
        )

    http_status = int(getattr(resp, "status", 0) or 0)
    final_url = str(getattr(resp, "url", "") or target)
    if http_status >= 400:
        # 4xx/5xx 是普通错误页：状态码本身证明不了"这是门户"
        return _probe_result(
            status.PROBE_UNKNOWN,
            kind=kind or "http-error",
            detail=f"候选地址返回 HTTP {http_status}（{redact_url(final_url)}），不是可用的认证页。",
            advice="请在「设置」里核对认证页面地址；如果它来自跳转，说明那不是学校门户",
            http_status=http_status,
            needs_confirm=True,
            manual_entry=True,
            candidate_url=redact_url(final_url),
        )

    page = PortalPage(resp.text, final_url)
    try:
        is_drcom = bool(looks_like_drcom(final_url, page))
    except Exception:  # noqa: BLE001
        is_drcom = False
    evidence = portal_page_evidence(page, is_drcom=is_drcom)
    try:
        form_count = len(page.forms)
    except Exception:  # noqa: BLE001
        form_count = 0
    title = (page.title or "").strip()
    evidence_text = "；".join(evidence.reasons[:4])

    if evidence.is_portal:
        return _probe_result(
            status.PROBE_PORTAL,
            kind=kind or "portal",
            detail="请求被校园网门户拦截。" + (f"证据：{evidence_text}" if evidence_text else ""),
            url=final_url,
            http_status=http_status,
            title=title,
            is_drcom=is_drcom,
            form_count=form_count,
            evidence=evidence.reasons,
            persistable=True,               # 只有拿到正向证据才允许写进配置
            from_redirect=from_redirect,
        )

    # 证据不足：绝不当作认证门户，也绝不写进配置（可能是普通网站 / 运营商页 / 代理页）
    if from_redirect:
        detail = (f"请求被跳转到 {redact_url(final_url)}，但该页面没有认证门户特征"
                  f"（{evidence_text}）。")
        advice = "不要把它当学校门户提交账号密码；请确认学校认证网址后手工填写"
    else:
        detail = (f"{redact_url(final_url)} 能打开（HTTP {http_status}），"
                  f"但页面没有认证门户特征（{evidence_text}）。")
        advice = "请在「设置」里核对并手工填写学校认证网址"
    return _probe_result(
        status.PROBE_UNKNOWN,
        kind=kind or (STATE_REDIRECT if from_redirect else STATE_HIJACK),
        detail=detail,
        advice=advice,
        http_status=http_status,
        title=title,
        is_drcom=is_drcom,
        form_count=form_count,
        evidence=evidence.reasons,
        needs_confirm=True,
        manual_entry=True,
        candidate_url=redact_url(final_url),
    )


def login(progress=None, stop: threading.Event | None = None) -> dict:
    """用已保存的账号密码做一次真实认证（和后台守护走同一套逻辑）。"""
    say = _make_sayer(progress)
    stopped = _make_stopper(stop)

    cfg = load_config()
    log = get_logger()

    if not getattr(cfg, "portal_auth_enabled", True):
        return {"ok": False, "status": "unsupported", "reason": "「连接后进行网页认证」已关闭，不提交认证。"}
    if not cfg.username:
        return {"ok": False, "status": "unsupported", "reason": "尚未填写学号 / 上网账号。请在「设置」中填写并保存。"}
    if not cfg.password_enc:
        return {"ok": False, "status": "unsupported", "reason": "尚未填写上网密码。请在「设置」中填写并保存。"}
    if not has_password(cfg):
        return {"ok": False, "status": "unsupported",
                "reason": "已保存的密码无法在本机解密（更换电脑或 Windows 用户后需重新填写）。"}
    password = crypto.unprotect(cfg.password_enc)

    client = build_client(cfg)
    say("正在检查当前网络状态…")
    if stopped():
        return {"ok": False, "status": "manual-required", "reason": "已停止", "stopped": True}
    try:
        state = check(client, cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "network-error", "reason": f"网络探测失败：{exc}"}
    if state.online:
        say("网络可用，无需认证。")
        return {"ok": True, "status": "already-online", "reason": "当前已经在线", "online": True}

    portal = state.portal_url or cfg.portal_url
    if not portal:
        return {
            "ok": False,
            "status": "network-error",
            "reason": "未找到认证页面地址。请在「认证」页点击「探测认证地址」，或在「设置」中填写。",
        }
    say(f"认证页面：{redact_url(portal)}")

    if stopped():
        return {"ok": False, "status": "manual-required", "reason": "已停止", "stopped": True}

    auth = PortalAuthenticator(client, cfg, log)
    outcome: AuthOutcome = auth.login(portal, cfg.username, password,
                                      make_verifier(client, cfg, log, tries=2))
    if outcome.ok:
        say(f"认证成功：{outcome.reason}")
        return {"ok": True, "status": outcome.status, "reason": outcome.reason,
                "strategy": outcome.strategy}
    say(f"认证失败：{outcome.reason}")
    return {"ok": False, "status": outcome.status, "reason": outcome.reason,
            "bad_credentials": outcome.bad_credentials, "needs_user": outcome.needs_user}


def test_connection(progress=None, stop: threading.Event | None = None) -> dict:
    """「测试连接」：用**已保存**的凭据走一遍完整链路，不修改任何配置。

    与「立即连接」的区别：这里不切换 Wi-Fi（不打断用户当前网络），只做
    "联网探测 → 必要时认证 → 复核外网"三步，用来回答"这套配置到底能不能上网"。
    返回值与 :func:`login` 同构，可直接交给 ``status.from_auth()`` 渲染。
    """
    cfg = load_config()
    say = _make_sayer(progress)

    if not cfg.username or not cfg.password_enc:
        return {
            "ok": False,
            "status": "unsupported",
            "reason": "还没有保存完整的账号与密码，无法测试。请先在「设置」里填写并保存。",
        }
    if not has_password(cfg):
        return {
            "ok": False,
            "status": "unsupported",
            "reason": "已保存的密码在本机解不开（换过 Windows 用户或电脑），请重新输入一次密码。",
        }

    say("正在检查当前网络状态…")
    result = login(progress=progress, stop=stop)
    result["tested"] = True
    result["tested_at"] = int(time.time())
    record_last_result(result)
    return result


# ------------------------------------------------ 最近一次连接结果（留痕）

LAST_RESULT_KEY = "last_result"


def record_last_result(result: dict) -> None:
    """把最近一次连接/测试的结构化结果记进 state.json（供界面"留痕"显示）。

    只记必要字段，**不记录密码、令牌、内网 IP**；失败也不抛出（留痕不是关键路径）。
    """
    try:
        from ..config import load_state, save_state

        payload = {
            "ok": bool(result.get("ok")),
            "status": str(result.get("status") or "")[:40],
            "reason": str(result.get("reason") or "")[:200],
            "tested": bool(result.get("tested")),
            "strategy": str(result.get("strategy") or "")[:60],
            "at": int(result.get("tested_at") or time.time()),
        }
        state = load_state()
        state[LAST_RESULT_KEY] = payload
        save_state(state)
    except Exception as exc:  # noqa: BLE001  留痕失败不影响主流程，但要留日志
        try:
            get_logger().debug("记录最近一次连接结果失败：%s", exc)
        except Exception:  # noqa: BLE001
            pass


def last_result() -> dict:
    """最近一次连接结果（没有则返回空字典）。"""
    try:
        from ..config import load_state

        value = load_state().get(LAST_RESULT_KEY)
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def last_result_text() -> tuple[str, int]:
    """最近一次结果的简短文案 + 时间戳（供面板显示）。"""
    data = last_result()
    if not data:
        return "还没有连接记录", 0
    ok = bool(data.get("ok"))
    label = "成功" if ok else "失败"
    reason = str(data.get("reason") or "").strip()
    if not ok and reason:
        label += f"：{reason[:40]}"
    return label, int(data.get("at") or 0)


def open_in_browser(url: str) -> bool:
    """在系统默认浏览器里打开认证页（用户想自己看一眼/手动操作时用）。"""
    if not url:
        return False
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001
        return False


def browser_login(progress=None, stop: threading.Event | None = None) -> dict:
    """浏览器模式：开窗口 → 验证来源 → 注入脚本填写 → 复核真实联网。"""
    cfg = load_config()
    if not getattr(cfg, "portal_auth_enabled", True):
        return {"ok": False, "status": "unsupported",
                "reason": "「连上后自动做网页认证」是关闭状态，不会提交认证"}
    if not cfg.username:
        return {"ok": False, "status": "unsupported", "reason": "尚未填写学号 / 上网账号。请在「设置」中填写。"}
    if not has_password(cfg):
        return {"ok": False, "status": "unsupported", "reason": "尚未保存可用的上网密码。请在「设置」中填写。"}
    try:
        password = crypto.unprotect(cfg.password_enc)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "unsupported", "reason": f"已保存的密码无法解密：{exc}"}

    from .. import webauth

    try:
        return webauth.run(cfg, password, progress=progress, stop=stop)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "server-error", "reason": f"浏览器认证异常：{exc}",
                "needs_user": True}


def close_browser() -> None:
    from .. import webauth

    webauth.close_active()


def connect_and_login(ssid: str, mode: str, progress=None,
                      stop: threading.Event | None = None) -> dict:
    """「立即连接」：先把 WiFi 连上，再做一次认证。"""
    cfg = load_config()
    target = (ssid or cfg.wifi_ssid or "").strip()
    say = _make_sayer(progress)

    if target:
        say(f"正在连接无线网络 {target}…")
        ok, msg = wifi.ensure_connected(target, mode or cfg.wifi_connect_mode, log=get_logger())
        if ok:
            say(f"已连接到 {target}。")
        else:
            say(f"连接失败：{msg}")
            return {"ok": False, "status": "network-error",
                    "reason": f"无线网络连接失败：{msg}", "wifi_ok": False}
    else:
        say("未设置目标网络，直接进行网页认证。")
    time.sleep(0.3)
    result = login(progress=progress, stop=stop)
    result["wifi_ok"] = True
    return result


# ------------------------------------------------ 后台守护与自启动


def heartbeat() -> dict:
    """后台守护的实时状态（没在跑就是空 dict）。"""
    return load_heartbeat()


def daemon_running() -> bool:
    """命名互斥体是否被持有 —— 只回答"进程在不在"，不代表它工作正常。"""
    return autostart.daemon_running()


def daemon_status() -> dict:
    """后台守护的真实健康状态：进程在不在 + 心跳是否新鲜 + 具体状态。

    界面和命令行都用它判断"后台是不是真的在工作"，避免出现
    "进程卡死但界面仍显示运行中"。
    """
    from ..daemon import daemon_health

    return daemon_health().as_dict()


def autostart_status() -> dict:
    return autostart.status_dict()


def set_autostart(enabled: bool) -> tuple[bool, str]:
    return autostart.enable() if enabled else autostart.disable()


def start_daemon() -> tuple[bool, str]:
    return autostart.start_daemon()


def stop_daemon() -> tuple[bool, str]:
    return autostart.stop_daemon()


# ------------------------------------------------ 诊断与文件


def data_dir() -> str:
    return str(paths.data_dir())


def open_path(target: str) -> bool:
    """用资源管理器打开目录 / 用默认程序打开文件。"""
    import os

    try:
        os.startfile(target)  # noqa: S606  (Windows 专用，界面按钮用)
        return True
    except Exception:  # noqa: BLE001
        return False


def log_tail(count: int = 40) -> list[str]:
    """读最近若干行日志（已脱敏，落盘时就是脱敏的）。"""
    path = paths.log_file()
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        return [line for line in text.splitlines() if line.strip()][-count:]
    except Exception:  # noqa: BLE001
        return []


def last_success() -> str:
    """上次认证成功的时间 / 策略（给界面显示）。"""
    from ..config import load_state

    data = load_state()
    ts = int(data.get("last_success_ts") or 0)
    if ts:
        import datetime

        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    return str(data.get("last_strategy") or "")


def security_report() -> dict:
    """只读的安全体检：杀毒软件现状 + 本程序的启动项 + 数据目录权限说明。

    V3.0 不再自动往 Defender 加排除项，这个函数只**读取**现状并给出建议，
    全程不需要管理员权限。
    """
    return diagnostics.security_report()


def export_diagnostics() -> tuple[bool, str]:
    """导出脱敏诊断包（账号、密码、内网地址都会被替换掉）。"""
    try:
        path = diagnostics.export_bundle()
        return True, str(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"导出失败：{exc}"


# ------------------------------------------------ 小工具


def _make_sayer(progress):
    def say(text: str) -> None:
        if progress is not None:
            try:
                progress(text)
            except Exception:  # noqa: BLE001
                pass

    return say


def _make_stopper(stop):
    def stopped() -> bool:
        return bool(stop is not None and stop.is_set())

    return stopped

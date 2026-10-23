"""联网/认证状态的统一表达（界面与命令行共用一份事实）。

## 为什么需要这个模块

3.0.x 的状态文字散落在各个页面里：有的地方读心跳、有的地方直接调 ``check()``、
有的地方只凭"无线接口存在"就写"已连接"。结果是同一个时刻不同控件说的话不一致，
而且**无法区分**"配置没填完""网络不可用""认证服务器没响应""认证失败"。

这个模块把"当前到底处于什么状态"收敛成唯一一份判定：

* 输入只来自真实业务结果：配置（是否已配置 / 是否读得出来）、
  ``checker.CheckResult``（联网探测）、``adapters.AuthOutcome`` 派生的字典（一次认证）、
  后台守护心跳（持续巡检）；
* 输出 :class:`ConnectionState`：一个 ``kind`` + 给用户看的 ``label`` +
  可执行 ``advice`` + 严重程度 ``severity``。

界面只负责渲染，不再自己拼状态文字，因此不会出现"保存失败仍显示成功"这类不一致。

## 判定原则（对应任务书"状态必须与真实业务结果一致"）

* 无线接口存在 **不代表** 能上网 —— 只有 ``CheckResult.online`` 才算 :data:`KIND_ONLINE`；
* HTTP 请求成功 **不代表** 账号认证成功 —— 认证成功必须同时满足"服务端语义成功 +
  外网真通"（由 ``AuthOutcome.ok`` 保证）；
* 原因未知时**不编造**原因：给 :data:`KIND_NETWORK_UNAVAILABLE` 并说明"无法确定具体原因"。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 状态种类

KIND_UNCONFIGURED = "unconfigured"            # 未配置账号
KIND_LOADING_CONFIG = "loading-config"        # 正在读取配置
KIND_CONFIG_ERROR = "config-error"            # 配置读取失败 / 配置不可用
KIND_CHECKING = "checking"                    # 正在检测网络
KIND_NEEDS_AUTH = "needs-auth"                # 探测到门户，尚未认证
KIND_AUTHENTICATING = "authenticating"        # 正在执行校园网认证
KIND_VERIFYING = "verifying"                  # 正在确认互联网连接
KIND_ONLINE = "online"                        # 连接成功
KIND_AUTH_FAILED = "auth-failed"              # 连接失败（认证未通过）
KIND_NEEDS_USER = "needs-user"                # 必须人工介入（验证码/浏览器）
KIND_NETWORK_UNAVAILABLE = "network-unavailable"   # 网络暂时不可用
KIND_PORTAL_UNREACHABLE = "portal-unreachable"     # 认证服务器无响应
KIND_INTERNAL_ERROR = "internal-error"        # 程序内部错误

SEV_GOOD = "good"
SEV_WARN = "warn"
SEV_BAD = "bad"
SEV_MUTED = "muted"

TERMINAL_KINDS = frozenset({
    KIND_ONLINE, KIND_AUTH_FAILED, KIND_NEEDS_USER,
    KIND_NETWORK_UNAVAILABLE, KIND_PORTAL_UNREACHABLE,
    KIND_CONFIG_ERROR, KIND_UNCONFIGURED, KIND_INTERNAL_ERROR,
})


@dataclass(frozen=True)
class ConnectionState:
    """一个可渲染的状态。``advice`` 必须是用户能照做的动作。"""

    kind: str
    label: str
    severity: str = SEV_MUTED
    detail: str = ""
    advice: str = ""
    source: str = ""          # config / check / auth / daemon / ui
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind == KIND_ONLINE

    @property
    def busy(self) -> bool:
        return self.kind in (KIND_LOADING_CONFIG, KIND_CHECKING,
                             KIND_AUTHENTICATING, KIND_VERIFYING)

    def describe(self) -> str:
        """一行完整描述：``标签：说明（建议）``。用于状态栏/日志。"""
        text = self.label
        if self.detail:
            text += f"：{self.detail}"
        if self.advice:
            text += f"（{self.advice}）"
        return text

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "severity": self.severity,
            "detail": self.detail,
            "advice": self.advice,
            "source": self.source,
        }


# ---------------------------------------------------------------- 构造器



def unconfigured(detail: str = "还没有填写 Wi-Fi 名称 / 学号 / 上网密码。") -> ConnectionState:
    return ConnectionState(
        kind=KIND_UNCONFIGURED,
        label="未配置账号",
        severity=SEV_WARN,
        detail=detail,
        advice="打开「设置」填写账号与密码并保存",
        source="config",
    )


def loading_config() -> ConnectionState:
    return ConnectionState(
        kind=KIND_LOADING_CONFIG,
        label="正在读取配置",
        severity=SEV_MUTED,
        source="config",
    )


def config_error(detail: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_CONFIG_ERROR,
        label="配置读取失败",
        severity=SEV_BAD,
        detail=detail or "配置文件无法解析或不可用。",
        advice="打开「设置」重新保存一次配置（原文件不会被自动覆盖）",
        source="config",
    )


def checking(detail: str = "正在检测网络连通性…") -> ConnectionState:
    return ConnectionState(
        kind=KIND_CHECKING, label="正在检测网络", severity=SEV_MUTED,
        detail=detail, source="check",
    )


def authenticating(detail: str = "正在向认证服务器提交账号密码…") -> ConnectionState:
    return ConnectionState(
        kind=KIND_AUTHENTICATING, label="正在执行校园网认证", severity=SEV_MUTED,
        detail=detail, source="auth",
    )


def verifying(detail: str = "正在确认互联网是否真的可用…") -> ConnectionState:
    return ConnectionState(
        kind=KIND_VERIFYING, label="正在确认互联网连接", severity=SEV_MUTED,
        detail=detail, source="check",
    )


def online(detail: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_ONLINE, label="连接成功", severity=SEV_GOOD,
        detail=detail or "外网探测返回了预期内容。", source="check",
    )


def needs_auth(portal: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_NEEDS_AUTH, label="需要校园网认证", severity=SEV_WARN,
        detail="当前被校园网门户拦截，尚未完成认证。" + (f"门户：{portal}" if portal else ""),
        advice="点「立即连接」会自动完成认证",
        source="check",
    )


def needs_user(detail: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_NEEDS_USER, label="需要你完成认证", severity=SEV_WARN,
        detail=detail or "本次认证需要人工操作（验证码或页面确认）。",
        advice="点「浏览器认证」在打开的窗口里完成",
        source="auth",
    )


def auth_failed(detail: str = "", advice: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_AUTH_FAILED, label="连接失败", severity=SEV_BAD,
        detail=detail or "认证没有通过。",
        advice=advice or "检查账号密码后重试；详情见「日志」",
        source="auth",
    )


def network_unavailable(detail: str = "", advice: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_NETWORK_UNAVAILABLE, label="网络暂时不可用", severity=SEV_WARN,
        detail=detail or "无法确定具体原因。",
        advice=advice or "确认已连接校园 Wi-Fi 后重试",
        source="check",
    )


def portal_unreachable(detail: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_PORTAL_UNREACHABLE, label="认证服务器没有响应", severity=SEV_WARN,
        detail=detail or "认证服务端返回异常或超时。",
        advice="稍后重试；若持续失败，确认校园网是否在维护或是否被安全软件拦截",
        source="auth",
    )


def internal_error(detail: str = "") -> ConnectionState:
    return ConnectionState(
        kind=KIND_INTERNAL_ERROR, label="程序内部错误", severity=SEV_BAD,
        detail=detail or "发生了未预期的错误。",
        advice="点「查看日志」把最近记录发给维护者；程序可继续使用",
        source="ui",
    )


# ---------------------------------------------------------------- 从真实结果映射

# 网络层错误 -> (说明, 建议)。键与 checker.STATE_* 一致。
_NETWORK_ERROR_TEXT = {
    "dns-error": ("域名解析失败。", "确认已连接校园 Wi-Fi；若 DNS 被拦截，稍后重试或改用 IP 探测"),
    "timeout": ("请求超时。", "网络可能不稳定或认证服务器繁忙，稍后重试"),
    "refused": ("连接被拒绝。", "校园网可能要求先认证，先点「立即连接」"),
    "tls-error": ("TLS 证书校验失败。", "可能是门户劫持 HTTPS；确认在校园网内后重试"),
    "proxy-error": ("系统代理设置异常。", "检查系统代理（设置 → 网络 → 代理）"),
    "unreachable": ("网络不可达。", "确认已连接校园 Wi-Fi 或插好网线"),
    "http-error": ("探测地址返回异常状态码。", "稍后重试；若一直失败请换探测地址"),
    "no-targets": ("配置里没有可用的探测地址。", "打开「设置 → 高级」恢复默认探测地址"),
}

_PORTAL_KINDS = frozenset({"redirect", "hijack"})


def _read(result, name: str, default=None):
    """同时支持 dict 与对象（CheckResult / ProgressResult）。"""
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def from_check(result) -> ConnectionState:
    """把一次联网探测结果翻译成状态。

    ``result`` 可以是 ``checker.CheckResult``，也可以是
    ``gui_service.online_status()`` 返回的字典。
    """
    online_flag = bool(_read(result, "online", False))
    kind = str(_read(result, "kind", "") or "")
    detail = str(_read(result, "detail", "") or "")
    portal = str(_read(result, "portal_url", "") or "")

    if online_flag:
        return online(detail or "外网探测返回了预期内容。")
    if kind in _PORTAL_KINDS:
        return needs_auth(portal)
    if kind in _NETWORK_ERROR_TEXT:
        text, advice = _NETWORK_ERROR_TEXT[kind]
        merged = text + (f"（{detail}）" if detail else "")
        return network_unavailable(merged, advice)
    # 未知原因：不编造
    return network_unavailable(
        detail or "探测未通过，但无法确定具体原因。",
        "确认已连接校园 Wi-Fi 后重试；若持续失败请查看日志",
    )


# ---------------------------------------------------------------- 探测认证门户

# 「探测认证地址」的三种结论。**只允许这三种**：
# ``ONLINE``（已确认能上外网，本来就不该有门户）、``PORTAL_SUSPECTED``（有证据
# 表明请求被门户拦截）、``UNKNOWN``（探测失败 / 证据不足 / 结果矛盾）。
#
# 为什么单独定义：3.1.0 的 ``gui_service.probe_portal()`` 只有一个布尔 ``ok``，
# 于是"已认证、网络正常、不需要门户"和"探测失败、无法判断"被压成同一个"失败"，
# 界面就对着一个能正常上网的用户报「探测失败 / 未探测到认证页面」（用户实际
# 反馈的故障）。这里把三种事实重新分开，界面只负责渲染。
PROBE_ONLINE = "online"                  # ONLINE：已确认能正常访问互联网
PROBE_PORTAL = "portal-suspected"        # PORTAL_SUSPECTED：有证据表明被门户拦截
PROBE_UNKNOWN = "unknown"                # UNKNOWN：探测失败 / 证据不足

PROBE_STATES = frozenset({PROBE_ONLINE, PROBE_PORTAL, PROBE_UNKNOWN})


def probe_online(detail: str = "") -> ConnectionState:
    """ONLINE：网络正常，当前没有需要认证的门户（不是错误）。"""
    return ConnectionState(
        kind=KIND_ONLINE,
        label="网络连接正常",
        severity=SEV_GOOD,
        detail=detail or "已确认能正常访问互联网，当前不需要认证门户。",
        advice="无需认证；若稍后掉线，再点一次「探测认证地址」即可",
        source="check",
    )


def probe_portal_found(portal: str = "", detail: str = "", advice: str = "") -> ConnectionState:
    """PORTAL_SUSPECTED：有明确证据表明被认证门户拦截/重定向。"""
    return ConnectionState(
        kind=KIND_NEEDS_AUTH,
        label="发现认证门户",
        severity=SEV_WARN,
        detail=detail or "请求被校园网门户拦截，需要完成认证。",
        advice=advice or "点「打开认证页面并填写」或「直接提交认证」完成认证",
        source="check",
        extra={"portal_url": portal} if portal else {},
    )


def probe_unknown(detail: str = "", advice: str = "", kind: str = "") -> ConnectionState:
    """UNKNOWN：探测失败或证据不足 —— 只能说"无法确定"，不能暗示已找到门户。

    ``kind`` 是 ``checker`` 的细分状态（dns-error / timeout / …），用来把
    "为什么无法确定"说清楚，并给出与该原因匹配的建议。
    """
    base = _NETWORK_ERROR_TEXT.get(kind)
    text = base[0] if base else ""
    if not advice and base:
        advice = base[1]
    merged = (text + (f"（{detail}）" if detail else "")) if text else ""
    merged = merged or detail or "探测未通过，且现有证据不足以判断是否需要认证。"
    return ConnectionState(
        kind=KIND_CONFIG_ERROR if kind == "no-targets" else KIND_NETWORK_UNAVAILABLE,
        label="无法确定认证状态",
        severity=SEV_WARN,
        detail=merged,
        advice=advice or "确认已连接校园 Wi-Fi 后重试；也可以手工填写认证页地址",
        source="check",
    )


def from_probe(result) -> ConnectionState:
    """把 ``gui_service.probe_portal()`` 的结果翻译成状态。

    兼容旧字段：缺 ``state`` 时按 ``ok`` 退化（``ok=True`` 视为发现了门户），
    这样旧的调用方不会因为这次修复而行为突变。
    """
    state = str(_read(result, "state", "") or "")
    if state not in PROBE_STATES:
        state = PROBE_PORTAL if _read(result, "ok", False) else PROBE_UNKNOWN
    detail = str(_read(result, "detail", "") or "")
    advice = str(_read(result, "advice", "") or "")
    kind = str(_read(result, "kind", "") or "")
    portal = str(_read(result, "portal_url", "") or _read(result, "url", "") or "")
    if state == PROBE_ONLINE:
        return probe_online(detail)
    if state == PROBE_PORTAL:
        return probe_portal_found(portal, detail, advice)
    return probe_unknown(detail, advice, kind)


# 认证状态 -> (标签类型, 说明, 建议)。键与 adapters.STATUS_* 一致。
_AUTH_STATUS_MAP = {
    "success": (KIND_ONLINE, "认证成功，且外网验证通过。", ""),
    "already-online": (KIND_ONLINE, "当前已经在线，无需重复认证。", ""),
    "bad-credentials": (KIND_AUTH_FAILED, "账号或密码不正确。",
                        "到「设置」重新填写账号与密码并保存（注意运营商后缀如 @cmcc）"),
    "captcha-required": (KIND_NEEDS_USER, "需要输入验证码才能完成认证。", ""),
    "manual-required": (KIND_NEEDS_USER, "这一步需要人工在页面上确认。", ""),
    "network-error": (KIND_NETWORK_UNAVAILABLE, "认证过程中的网络请求失败。", ""),
    "server-error": (KIND_PORTAL_UNREACHABLE, "认证服务器没有正常响应。", ""),
    "unsupported": (KIND_CONFIG_ERROR, "找不到可用的认证方式或缺配置。",
                    "在「设置」里补全账号密码，或到「认证」页探测认证地址"),
    "refused-target": (KIND_AUTH_FAILED, "认证页地址未通过校验，已拒绝提交密码。",
                       "在「设置」里确认认证页地址是否正确"),
    "verify-failed": (KIND_AUTH_FAILED, "服务端说认证成功，但外网仍不通。",
                      "稍后重试；若一直失败可能是双出口问题，请查看日志"),
}


def from_auth(result) -> ConnectionState:
    """把一次认证结果（``gui_service.login()`` 的字典）翻译成状态。"""
    ok = bool(_read(result, "ok", False))
    status = str(_read(result, "status", "") or "")
    reason = str(_read(result, "reason", "") or "")

    if ok:
        kind, text, _advice = _AUTH_STATUS_MAP.get(status, (KIND_ONLINE, reason, ""))
        return ConnectionState(
            kind=kind, label="连接成功", severity=SEV_GOOD,
            detail=reason or text, source="auth",
        )
    mapped = _AUTH_STATUS_MAP.get(status)
    if mapped is None:
        return auth_failed(reason or "认证未通过，且无法确定具体原因。")
    kind, text, advice = mapped
    detail = reason or text
    if kind == KIND_NEEDS_USER:
        return needs_user(detail)
    if kind == KIND_NETWORK_UNAVAILABLE:
        return network_unavailable(detail)
    if kind == KIND_PORTAL_UNREACHABLE:
        return portal_unreachable(detail)
    if kind == KIND_CONFIG_ERROR:
        return config_error(detail)
    return auth_failed(detail, advice)


# 后台守护心跳状态 -> 状态种类（用于"长期巡检视角"）
_HEARTBEAT_MAP = {
    "starting": (KIND_CHECKING, "正在启动", SEV_MUTED),
    "checking": (KIND_CHECKING, "正在检测网络", SEV_MUTED),
    "online": (KIND_ONLINE, "连接成功", SEV_GOOD),
    "wifi": (KIND_CHECKING, "正在连接 Wi-Fi", SEV_MUTED),
    "auth": (KIND_AUTHENTICATING, "正在执行校园网认证", SEV_MUTED),
    "cooldown": (KIND_NETWORK_UNAVAILABLE, "正在等待重试", SEV_WARN),
    "need-user": (KIND_NEEDS_USER, "需要你处理", SEV_WARN),
    "paused": (KIND_UNCONFIGURED, "已暂停自动连接", SEV_MUTED),
    "stopped": (KIND_NETWORK_UNAVAILABLE, "后台守护已停止", SEV_MUTED),
}


def from_heartbeat(beat) -> ConnectionState | None:
    """把后台守护心跳翻译成状态；心跳为空时返回 ``None``（表示没有信息）。"""
    if not beat:
        return None
    state = str(_read(beat, "state", "") or "")
    detail = str(_read(beat, "detail", "") or "")
    mapped = _HEARTBEAT_MAP.get(state)
    if mapped is None:
        return ConnectionState(
            kind=KIND_CHECKING, label="后台守护状态未知", severity=SEV_MUTED,
            detail=detail, source="daemon",
        )
    kind, label, severity = mapped
    advice = ""
    if kind == KIND_NEEDS_USER:
        advice = "打开「认证」页点「浏览器认证」完成人工步骤"
    elif kind == KIND_NETWORK_UNAVAILABLE and state == "cooldown":
        advice = "后台守护会自动重试；也可点「立即连接」立刻重试"
    elif state == "stopped":
        advice = "点「设置 → 启动与安全」里的按钮可重新启动后台守护"
    return ConnectionState(
        kind=kind, label=label, severity=severity,
        detail=detail, advice=advice, source="daemon",
    )

"""联网状态判定 + captive portal 探测（V3.0 做了状态细分）。

核心思路（和 Windows NCSI 同款）：去请求一个"只有真正连通外网才会返回预期内容"
的地址，再根据结果分类：

====================  ============================================================
状态                  含义
====================  ============================================================
``online``            探测目标返回了预期内容，什么都不用做
``redirect``          被 302 到别的地址 —— 那个 Location 就是认证页
``hijack``            200 但内容是门户页 —— 流量被劫持，页面本身就是认证页
``dns-error``         DNS 解析失败（未认证时很常见，也可能是真的没网）
``timeout``           连接超时
``refused``           连接被拒绝（本机没有到目标的出口）
``tls-error``         证书/握手失败（多半是自签门户没放行）
``proxy-error``       代理拦截
``unreachable``       其它网络不可达
``http-error``        服务端返回非预期状态码（≠ 认证失败）
``no-targets``        配置里没有可用的探测地址
====================  ============================================================

V3.0 明确区分这些情况，是为了不再把"DNS 挂了 / 探测站点临时不可用"统统报成
「认证失败」。上层 :mod:`campusnet.daemon` 只对 ``redirect`` / ``hijack`` 触发
认证，对其余状态做退避重试或直接给用户可操作的提示。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field

from .config import Config
from .http import HttpClient, Response, classify_network_error

STATE_ONLINE = "online"
STATE_REDIRECT = "redirect"
STATE_HIJACK = "hijack"
STATE_DNS = "dns-error"
STATE_TIMEOUT = "timeout"
STATE_REFUSED = "refused"
STATE_TLS = "tls-error"
STATE_PROXY = "proxy-error"
STATE_UNREACHABLE = "unreachable"
STATE_HTTP = "http-error"
STATE_NO_TARGETS = "no-targets"

# classify_network_error() 用的是短名字，这里映射到对外状态名
_ERROR_KIND_MAP = {
    "dns": STATE_DNS,
    "timeout": STATE_TIMEOUT,
    "refused": STATE_REFUSED,
    "tls": STATE_TLS,
    "proxy": STATE_PROXY,
    "unreachable": STATE_UNREACHABLE,
}

# 这些状态意味着"网络层本身有问题"，不该把它当成认证失败
NETWORK_LAYER_KINDS = frozenset(
    {STATE_DNS, STATE_TIMEOUT, STATE_REFUSED, STATE_TLS, STATE_PROXY, STATE_UNREACHABLE, STATE_NO_TARGETS}
)
# 这些状态意味着"能到网关/门户，只是还没认证"
PORTAL_KINDS = frozenset({STATE_REDIRECT, STATE_HIJACK})

_KIND_HINT = {
    STATE_ONLINE: "网络已可用，无需认证。",
    STATE_REDIRECT: "网络被重定向到校园网认证页面，需要完成认证。",
    STATE_HIJACK: "访问外部网站被替换成认证页面，需要完成认证。",
    STATE_DNS: "域名解析失败：可能还没通过认证（校园网常先拦 DNS），也可能是没连上网络。",
    STATE_TIMEOUT: "网络请求超时：确认已连接校园网；如果开了代理/VPN 请先关掉再试。",
    STATE_REFUSED: "连接被拒绝：本机到外网没有可用出口，通常表示 Wi-Fi 未连上或需要重新认证。",
    STATE_TLS: "TLS 证书校验失败：学校门户多使用自签证书，可在设置里把该门户地址加入放行列表。",
    STATE_PROXY: "请求被代理拦截：请关闭系统代理 / VPN 后重试。",
    STATE_UNREACHABLE: "网络不可达：可能没连上 Wi-Fi、网线未插，或校园网要求先认证。",
    STATE_HTTP: "探测目标返回了非预期状态码：可能是探测站点临时故障，稍后会自动重试。",
    STATE_NO_TARGETS: "配置里没有可用的联网探测地址，无法判断网络状态。",
}


def describe_kind(kind: str) -> str:
    return _KIND_HINT.get(kind, "网络状态未知。")


@dataclass
class CheckResult:
    """一次联网探测的结果。``kind`` 是细分状态，旧代码里的 ``reason`` 保留兼容。"""

    online: bool
    portal_url: str = ""
    kind: str = STATE_UNREACHABLE
    detail: str = ""
    page_html: str = ""
    http_status: int = 0
    tried: list = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def reason(self) -> str:
        """旧字段名（日志里一直在用），等价于 ``kind``。"""
        return self.kind

    @property
    def is_network_layer(self) -> bool:
        return self.kind in NETWORK_LAYER_KINDS

    @property
    def needs_portal_auth(self) -> bool:
        return (not self.online) and self.kind in PORTAL_KINDS

    def explain(self) -> str:
        """给用户看的一句话：发生了什么 + 建议怎么处理。"""
        text = describe_kind(self.kind)
        if self.detail and self.kind != STATE_ONLINE:
            return f"{text}（{self.detail}）"
        return text

    def __bool__(self) -> bool:
        return self.online


def _matches(resp: Response, target) -> bool:
    if target.expect_status and resp.status == target.expect_status:
        if not target.expect_text:
            return True
    if resp.status != 200 and not target.expect_status:
        return False
    if target.expect_text:
        return target.expect_text.lower() in resp.text.lower()
    return resp.status == 200


def _looks_like_html(resp: Response) -> bool:
    ctype = resp.content_type.lower()
    if "text/html" in ctype:
        return True
    head = resp.body[:8192].lower()
    return b"<html" in head or b"<form" in head or b"<!doctype" in head


def check(client: HttpClient, cfg: Config, *, timeout: float | None = None) -> CheckResult:
    """按 check_urls 逐个探测，返回**首个确定性结论**。

    设计取舍：一个探测目标失败不等于没网（学校可能只放行部分域名），所以要
    遍历全部目标；只有所有目标都不匹配时，才把最有信息量的那条错误当作结论。
    """
    started = time.time()
    tried: list[dict] = []
    first_error_kind = ""
    first_error_detail = ""
    last_http_status = 0
    portal_from_redirect = ""
    portal_from_hijack = ""
    hijack_html = ""
    hijack_status = 0

    targets = cfg.targets()
    if not targets:
        return CheckResult(
            online=False, kind=STATE_NO_TARGETS,
            detail="check_urls 为空", elapsed=time.time() - started,
        )

    for target in targets:
        try:
            resp = client.request(
                target.url,
                method="GET",
                follow_redirects=False,
                timeout=timeout or cfg.request_timeout,
            )
        except urllib.error.HTTPError as exc:  # 少见：非重定向类 HTTP 错误
            last_http_status = exc.code
            tried.append({"url": target.url, "error": f"HTTP {exc.code}"})
            if not first_error_kind:
                first_error_kind = STATE_HTTP
                first_error_detail = f"{target.url} -> HTTP {exc.code}"
            continue
        except Exception as exc:  # noqa: BLE001  URLError/socket.timeout/ssl 等
            kind = _ERROR_KIND_MAP.get(classify_network_error(exc), STATE_UNREACHABLE)
            message = str(getattr(exc, "reason", exc))
            tried.append({"url": target.url, "error": message, "kind": kind})
            if not first_error_kind or first_error_kind == STATE_UNREACHABLE:
                first_error_kind = kind
                first_error_detail = f"{target.url} -> {message}"
            continue

        last_http_status = resp.status
        tried.append({"url": target.url, "status": resp.status, "final": resp.url})

        if _matches(resp, target):
            return CheckResult(
                online=True,
                kind=STATE_ONLINE,
                detail=f"{target.url} 返回预期内容",
                http_status=resp.status,
                tried=tried,
                elapsed=time.time() - started,
            )

        if resp.is_redirect:
            if not portal_from_redirect:
                portal_from_redirect = urllib.parse.urljoin(resp.url, resp.location)
            continue

        if resp.status == 200 and resp.body and _looks_like_html(resp):
            if not portal_from_hijack:
                portal_from_hijack = resp.url
                hijack_html = resp.text
                hijack_status = resp.status
            continue

        tried[-1]["note"] = "响应不匹配预期"
        if not first_error_kind:
            first_error_kind = STATE_HTTP
            first_error_detail = f"{target.url} -> HTTP {resp.status}"

    if portal_from_redirect or portal_from_hijack:
        return CheckResult(
            online=False,
            portal_url=portal_from_redirect or portal_from_hijack,
            kind=STATE_REDIRECT if portal_from_redirect else STATE_HIJACK,
            detail="探测到认证跳转" if portal_from_redirect else "探测到内容劫持",
            page_html=hijack_html,
            http_status=hijack_status,
            tried=tried,
            elapsed=time.time() - started,
        )

    return CheckResult(
        online=False,
        kind=first_error_kind or STATE_UNREACHABLE,
        detail=first_error_detail or "所有探测目标均不可达",
        http_status=last_http_status,
        tried=tried,
        elapsed=time.time() - started,
    )


def portal_reachable(client: HttpClient, portal_url: str, timeout: float = 4) -> bool:
    """认证页本身是否可达（用来区分"没网"和"没认证"）。"""
    if not portal_url:
        return False
    try:
        client.request(portal_url, method="GET", follow_redirects=False, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False

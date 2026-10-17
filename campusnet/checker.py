"""联网状态判定 + captive portal 探测。

核心思路（和 Windows NCSI 同款）：去请求一个"只有真正连通外网才会返回
预期内容"的地址。
- 拿到预期内容   -> 已经在线，什么都不用做
- 被 302 到别的地址 -> 那个 Location 就是认证页
- 200 但内容是别的东西 -> 流量被劫持，直接拿这次响应当认证页
- 连不上（DNS/超时） -> 可能没插网线/没连 WiFi，也可能未认证导致 DNS 不可用
"""

from __future__ import annotations

import urllib.error
import urllib.parse
from dataclasses import dataclass, field

from .config import Config
from .http import HttpClient, Response


@dataclass
class CheckResult:
    online: bool
    portal_url: str = ""
    reason: str = "ok"
    detail: str = ""
    page_html: str = ""
    tried: list = field(default_factory=list)

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


def check(client: HttpClient, cfg: Config, *, timeout: float | None = None) -> CheckResult:
    """按 check_urls 逐个探测，返回首个确定性结论。"""
    tried: list[dict] = []
    network_error: str = ""
    portal_from_redirect = ""
    portal_from_hijack = ""
    hijack_html = ""

    for target in cfg.targets():
        try:
            resp = client.request(
                target.url,
                method="GET",
                follow_redirects=False,
                timeout=timeout or cfg.request_timeout,
            )
        except urllib.error.HTTPError as exc:  # 少见：非重定向类 HTTP 错误
            network_error = network_error or f"{target.url} -> HTTP {exc.code}"
            tried.append({"url": target.url, "error": f"HTTP {exc.code}"})
            continue
        except Exception as exc:  # noqa: BLE001  URLError/socket.timeout/ssl 等
            msg = getattr(exc, "reason", exc)
            network_error = network_error or f"{target.url} -> {msg}"
            tried.append({"url": target.url, "error": str(msg)})
            continue

        tried.append({"url": target.url, "status": resp.status, "final": resp.url})

        if _matches(resp, target):
            return CheckResult(online=True, reason="ok", detail=f"{target.url} 返回预期内容", tried=tried)

        if resp.status in (301, 302, 303, 307, 308) and resp.location:
            if not portal_from_redirect:
                portal_from_redirect = urllib.parse.urljoin(resp.url, resp.location)
            continue

        if resp.status == 200 and resp.body:
            ctype = resp.content_type.lower()
            if "text/html" in ctype or b"<html" in resp.body[:2048].lower() or b"<form" in resp.body[:8192].lower():
                if not portal_from_hijack:
                    portal_from_hijack = resp.url
                    hijack_html = resp.text
                continue

        tried[-1]["note"] = "响应不匹配预期"

    if portal_from_redirect or portal_from_hijack:
        return CheckResult(
            online=False,
            portal_url=portal_from_redirect or portal_from_hijack,
            reason="redirect" if portal_from_redirect else "hijack",
            detail="探测到认证跳转" if portal_from_redirect else "探测到内容劫持",
            page_html=hijack_html,
            tried=tried,
        )

    return CheckResult(
        online=False,
        portal_url="",
        reason="unreachable",
        detail=network_error or "所有探测目标均不可达",
        tried=tried,
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

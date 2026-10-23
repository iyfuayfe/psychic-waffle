"""安全基元：凭据脱敏、URL 来源校验、认证页转储的净化。

这个模块只做"纯函数"，不碰网络也不读配置，方便单元测试全覆盖。

三条规矩：

1. **日志与转储里永远不出现明文密码**。所有可能带凭据的字符串都要过
   :func:`redact_text` / :func:`redact_url`。
2. **账号密码只允许提交给已确认的地址**。判定集中在 :class:`UrlPolicy`，
   任何新的适配器都必须走它，不允许各写一套。
3. **认证页转储默认关闭**，打开时也必须先净化（见 :func:`redact_portal_html`）。
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field

# ------------------------------------------------------------------ 账号脱敏

_MOBILE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_MAC_RE = re.compile(r"(?<![0-9a-fA-F])((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})(?![0-9a-fA-F])")
# "ss4='000000000000'" / mac=0a1b2c3d4e5f：Dr.COM 门户里 MAC 是 12 位无分隔十六进制
_MAC_KEYED_RE = re.compile(
    r"(?i)(\b(?:ss4|mac|bssid|wlan_user_mac|wlanusermac)\b\s*[=:]\s*['\"]?)"
    r"(?:[0-9a-f]{12}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})"
)
_IPV4_RE = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")

# 敏感字段名（出现在 URL 查询串 / 表单体 / JS 赋值里）
_SENSITIVE_KEYS = (
    "password", "passwd", "pwd", "psw", "upass", "ddddd", "pass",
    "user_password", "old_password", "new_password",
    "token", "access_token", "refresh_token", "sessionid", "session_id",
    "jsessionid", "phpsessid", "rcn", "secret", "apikey", "api_key",
    "auth", "signature", "sign",
)
_KEY_ALT = "|".join(re.escape(k) for k in sorted(_SENSITIVE_KEYS, key=len, reverse=True))

# key=value（查询串、表单体、JSON 片段、JS 赋值都覆盖）
_KV_RE = re.compile(
    rf"(?P<key>\b(?:{_KEY_ALT})\b)\s*(?P<sep>[=:])\s*"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s&;,\"'<>]{1,200})",
    re.I,
)
# uid='xxx@cmcc' / account='xxx' 这类认证页里的账号变量
_ACCOUNT_ASSIGN_RE = re.compile(
    r"(?P<key>\b(?:uid|userid|user_id|account|username|user_account|acct)\b\s*[=:]\s*)"
    r"(?P<q>['\"])(?P<val>[^'\"]{1,80})(?P=q)",
    re.I,
)
# 32 字节以上的 base64 / 十六进制串（AES 密文、令牌、哈希）
_LONG_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

MASK = "***"


def mask_account(account: str) -> str:
    """日志里显示账号：保留前 3 后 4，中间打码（与旧版日志格式兼容）。"""
    text = str(account or "")
    if not text:
        return ""
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{mask_account(local)}@{domain}"
    if len(text) <= 6:
        return text[:2] + MASK
    return text[:3] + "****" + text[-4:]


def mask_ipv4(text: str) -> str:
    """把 IPv4 的后两段打码，保留网段信息（诊断够用，定位不了具体设备）。"""

    def _sub(match: re.Match) -> str:
        value = match.group(1)
        parts = value.split(".")
        return ".".join(parts[:2] + [MASK, MASK])

    return _IPV4_RE.sub(_sub, text or "")


def mask_mac(text: str) -> str:
    return _MAC_RE.sub(lambda m: m.group(1)[:8] + ":******", text or "")


def mask_mobile(text: str) -> str:
    return _MOBILE_RE.sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], text or "")


def redact_text(text: str, *, keep_ip: bool = True) -> str:
    """把任意文本里的凭据/账号/令牌替换成掩码。

    用于日志、异常消息、GUI 提示、诊断包；保持可读性（不整段丢弃），
    这样"出了什么问题"仍然看得懂。
    """
    if not text:
        return ""
    out = str(text)

    def _kv(match: re.Match) -> str:
        key, sep, val = match.group("key"), match.group("sep"), match.group("val")
        quote = ""
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            quote = val[0]
            val = val[1:-1]
        if val in ("", MASK, "None", "null"):
            return f"{key}{sep}{quote}{val}{quote}"
        return f"{key}{sep}{quote}{MASK}{quote}"

    out = _KV_RE.sub(_kv, out)

    def _acct(match: re.Match) -> str:
        return f"{match.group('key')}{match.group('q')}{mask_account(match.group('val'))}{match.group('q')}"

    out = _ACCOUNT_ASSIGN_RE.sub(_acct, out)
    out = mask_mobile(out)
    out = _LONG_B64_RE.sub(lambda m: m.group(0)[:6] + "…" + MASK, out)
    out = _LONG_HEX_RE.sub(lambda m: m.group(0)[:6] + "…" + MASK, out)
    if not keep_ip:
        out = mask_ipv4(out)
        out = mask_mac(out)
    return out


# ------------------------------------------------------------------ URL 脱敏

_SAFE_QUERY_KEYS = {"lang", "callback_name", "jsversion", "t", "v", "format"}


def redact_url(url: str, *, keep_ip: bool = False) -> str:
    """保留 scheme/host/path，查询串里的值全部打码（只留参数名）。"""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except Exception:  # noqa: BLE001
        return redact_text(url, keep_ip=keep_ip)
    host = parts.hostname or ""
    if not keep_ip and host:
        try:
            host = mask_ipv4(host) if not ipaddress.ip_address(host).is_loopback else host
        except ValueError:
            pass
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    if parts.username:
        netloc = f"{MASK}@{netloc}"
    query = ""
    if parts.query:
        pairs = []
        for key, _value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in _SAFE_QUERY_KEYS:
                pairs.append(f"{key}=…")
            else:
                pairs.append(f"{key}={MASK}")
        query = "?" + "&".join(pairs)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query.lstrip("?"), ""))


def redact_portal_html(html: str, url: str = "") -> str:
    """净化认证页转储：去掉账号、密码、令牌、内网地址、MAC。

    转储的价值在于"这个学校的门户长什么样"，账号与地址对内网定位没有帮助，
    反而会在分享、备份时泄露信息，所以默认全部打码。
    """
    body = html or ""
    body = _ACCOUNT_ASSIGN_RE.sub(
        lambda m: f"{m.group('key')}{m.group('q')}{mask_account(m.group('val'))}{m.group('q')}",
        body,
    )

    def _kv(match: re.Match) -> str:
        key, sep = match.group("key"), match.group("sep")
        return f"{key}{sep}{MASK}"

    body = _KV_RE.sub(_kv, body)
    body = _MAC_KEYED_RE.sub(lambda m: m.group(1) + MASK, body)
    body = mask_ipv4(body)
    body = mask_mac(body)
    body = mask_mobile(body)
    body = _LONG_B64_RE.sub(lambda m: m.group(0)[:6] + "…" + MASK, body)
    header = f"<!-- redacted portal dump: {redact_url(url)} -->\n" if url else ""
    return header + body


# ------------------------------------------------------------------ URL 策略

_ALLOWED_SCHEMES = ("http", "https")


def host_key(url: str) -> str:
    """归一化的主机名（小写、去掉尾部点号、**不含端口**）。

    为什么去掉端口：校园网门户常用"网页在 80、认证接口在 801/8080"的部署，
    甚至同一台机器上 80 与 801 是两个不同的服务。对"能不能把密码发给它"
    这个问题来说，主机（域名 / IP）才是判据，端口不改变归属。
    需要严格比较端口时用 :func:`origin_of`。
    """
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except Exception:  # noqa: BLE001
        return ""
    host = (parts.hostname or "").rstrip(".").lower()
    return host


def origin_of(url: str) -> str:
    """``scheme://host[:port]``；不可解析时返回空串。"""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except Exception:  # noqa: BLE001
        return ""
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        return ""
    host = parts.hostname.rstrip(".").lower()
    if parts.port:
        return f"{scheme}://{host}:{parts.port}"
    return f"{scheme}://{host}"


def is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except Exception:  # noqa: BLE001
        return False
    return (parts.scheme or "").lower() in _ALLOWED_SCHEMES and bool(parts.hostname)


def is_https(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(str(url or "")).scheme.lower() == "https"
    except Exception:  # noqa: BLE001
        return False


def is_loopback(url: str) -> bool:
    """是不是本机回环地址（CDP 调试口、本地模拟门户）。"""
    host = host_key(url).split(":")[0]
    if not host:
        return False
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def same_host(a: str, b: str) -> bool:
    """同一主机（只看主机名，不看端口 —— 见 :func:`host_key` 的说明）。"""
    ka, kb = host_key(a), host_key(b)
    return bool(ka) and ka == kb


def same_origin(a: str, b: str) -> bool:
    oa, ob = origin_of(a), origin_of(b)
    return bool(oa) and oa == ob


def keeps_portal_query(old_url: str, new_url: str) -> bool:
    """新地址是否保住了旧地址的门户参数（防止"参数退化"写回配置）。

    背景（3.1.0 实测）：探测门户时如果跟随了重定向，拿到的是门户的"最终页地址"，
    常常不带 ``wlanuserip`` / ``wlanacname`` / ``nasip`` 等网关参数；而发现阶段的
    跳转地址是带这些参数的。用前者覆盖后者会让后续认证丢参数（本机日志里
    配置的门户地址就这样从"带完整参数"变成了裸 ``a79.htm``）。

    只在"同一主机、旧地址有查询串、新地址没有查询串"时判为退化；
    其余情况一律返回 True（允许更新），避免挡住正常更新。
    """
    if not old_url or not new_url:
        return True
    if not same_host(old_url, new_url):
        return True
    try:
        old_query = urllib.parse.urlsplit(str(old_url)).query
        new_query = urllib.parse.urlsplit(str(new_url)).query
    except Exception:  # noqa: BLE001
        return True
    return not (old_query and not new_query)


@dataclass
class Decision:
    """一次来源校验的结论。``allowed=False`` 时 ``reason`` 必须能直接给用户看。"""

    allowed: bool
    reason: str = ""
    host: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class UrlPolicy:
    """决定"账号密码能不能提交到这个地址"。

    允许的三种情况（任一即可）：

    1. 目标 host 与**当前认证页**一致（表单 action 指向自身最常见）；
    2. 目标 host 与**已保存 / 本次探测到的认证页**一致；
    3. 目标 host 在配置的 ``trusted_auth_hosts`` 里（学校把认证接口放在另一个域名）。

    除此之外一律拒绝，并把原因交回调用方 —— 宁可让用户手动点一下，也不能把
    密码发给没有确认过的第三方地址。
    """

    def __init__(self, trusted_hosts: list | None = None):
        self._trusted = []
        for item in trusted_hosts or []:
            key = self._normalize(item)
            if key:
                self._trusted.append(key)

    @staticmethod
    def _normalize(value: str) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if "://" in text or ":" in text:
            # 允许直接写 "https://auth.school.edu.cn" 或 "auth.school.edu.cn:8443"
            return host_key(text) or text.split(":")[0]
        return text.rstrip(".")

    def trusted_hosts(self) -> list:
        return list(self._trusted)

    def allow(self, target_url: str, *sources: str) -> Decision:
        if not is_http_url(target_url):
            return Decision(False, "目标地址不是合法的 http/https 地址", "")
        host = host_key(target_url)
        for source in sources:
            if source and same_host(source, target_url):
                return Decision(True, "", host)
        if host and host in self._trusted:
            return Decision(True, "", host)
        return Decision(
            False,
            f"认证接口位于未确认的地址（{host or '未知主机'}）。"
            "为避免账号密码被发送到意料之外的网站，已停止自动提交；"
            "如果确认这是学校自己的认证接口，请在配置的 trusted_auth_hosts 中加入该地址。",
            host,
        )

    def allow_portal(self, portal_url: str) -> Decision:
        """认证页本身是否可信（用于自动填写前的二次确认）。

        只要求是合法 http/https；真正的同源校验在页面脚本里用 origin 精确比对，
        这里额外拒绝"看起来像钓鱼"的 scheme。
        """
        if not is_http_url(portal_url):
            return Decision(False, f"认证页地址不合法：{portal_url!r}", "")
        scheme = urllib.parse.urlsplit(portal_url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return Decision(False, f"不支持的协议：{scheme}", host_key(portal_url))
        return Decision(True, "", host_key(portal_url))


@dataclass
class RedactionStats:
    """诊断包生成时统计"打掉了多少东西"，便于向用户说明。"""

    fields: int = 0
    notes: list = field(default_factory=list)

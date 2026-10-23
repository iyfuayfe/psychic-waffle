"""极简 HTTP 客户端（纯标准库）。

针对校园网场景做的几件特殊处理：

1. 默认**关闭系统代理**：如果本机跑着 Clash / v2ray，探测流量会被代理接走，
   结果就是"明明没认证却显示已联网"，自动登录永远不触发。
2. 默认**不跟随重定向**：captive portal 的识别就是靠 302 的 Location，
   跟过去了就丢掉了唯一的线索。
3. 中文门户大量使用 GBK，所以这里做了 charset 嗅探。

V3.0 的安全改动：

* **TLS 证书默认校验**。需要放行的自签证书主机必须在配置里点名
  （``tls_insecure_hosts``），由 :func:`campusnet.config.Config.tls_verify_for`
  逐主机决定，不再"全局关校验"。
* **跨主机重定向不重发请求体**。307/308 会保留 POST body，如果目标主机换了，
  账号密码就会被送到另一个站点 —— 这里直接停止跟随并交回 3xx 响应。
* **响应体大小上限**，避免恶意/异常门户把内存打满。
* 所有网络层错误都带上可区分的原因，供 :mod:`campusnet.checker` 分类。
"""

from __future__ import annotations

import gzip
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.cookiejar import CookieJar
from typing import Callable

from .config import DEFAULT_USER_AGENT
from .security import host_key

_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2 MB：门户页够用，异常响应拦住


class Response:
    __slots__ = ("status", "headers", "body", "url", "_text", "elapsed")

    def __init__(self, status: int, headers, body: bytes, url: str, elapsed: float = 0.0):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url
        self.elapsed = elapsed
        self._text: str | None = None

    @property
    def content_type(self) -> str:
        try:
            return self.headers.get("Content-Type", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    @property
    def location(self) -> str:
        try:
            return self.headers.get("Location", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = decode_body(self.body, self.content_type)
        return self._text

    @property
    def is_redirect(self) -> bool:
        return self.status in _REDIRECT_CODES and bool(self.location)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Response {self.status} {self.url} {len(self.body)}B>"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """把 3xx 当成普通响应交回来，而不是自动跟过去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _maybe_decompress(raw: bytes, headers) -> bytes:
    enc = ""
    try:
        enc = (headers.get("Content-Encoding", "") or "").lower()
    except Exception:  # noqa: BLE001
        return raw
    if not raw or not enc:
        return raw
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001
        return raw
    return raw


def guess_encoding(content_type: str | None, raw: bytes) -> str:
    if content_type:
        m = _CHARSET_RE.search(content_type.encode("latin-1", "ignore"))
        if m:
            return m.group(1).decode("ascii", "ignore").lower()
    m = _CHARSET_RE.search(raw[:4096])
    if m:
        return m.group(1).decode("ascii", "ignore").lower()
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gbk"


def decode_body(raw: bytes, content_type: str | None = None) -> str:
    if not raw:
        return ""
    order = [guess_encoding(content_type, raw), "utf-8", "gbk", "big5"]
    seen = set()
    for enc in order:
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


def is_cross_origin(a: str, b: str) -> bool:
    return _origin(a) != _origin(b)


def _same_origin_strict(a: str, b: str) -> bool:
    """严格的同源判定：scheme + host + port 全都要一样。

    用在"307/308 要不要重发请求体"这个决定上：请求体里可能是账号密码，
    只要目标端点换了（哪怕只是换了端口），就不重发。
    """

    def key(url: str):
        try:
            parts = urllib.parse.urlsplit(url)
        except Exception:  # noqa: BLE001
            return ("", "", 0)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port or {"http": 80, "https": 443}.get(scheme, 0)
        return (scheme, host, port)

    return key(a) == key(b)


class HttpClient:
    """带 Cookie 会话的极简客户端。

    ``tls_verify`` 是 ``callable(url) -> bool``：为 True 表示该地址要校验证书。
    传入 ``None`` 时退回全局 ``insecure_tls`` 开关（兼容旧调用）。
    """

    def __init__(
        self,
        *,
        timeout: float = 8,
        insecure_tls: bool = False,
        use_system_proxy: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
        tls_verify: Callable[[str], bool] | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ):
        self.timeout = timeout
        self.jar = CookieJar()
        self.tls_verify = tls_verify
        self.max_bytes = int(max_bytes)
        self.user_agent = user_agent

        self._handlers_base: list = [urllib.request.HTTPCookieProcessor(self.jar)]
        if not use_system_proxy:
            self._handlers_base.append(urllib.request.ProxyHandler({}))
        self._handlers_base.append(_NoRedirect())

        self._verified_ctx = ssl.create_default_context()
        self._insecure_ctx = ssl.create_default_context()
        self._insecure_ctx.check_hostname = False
        self._insecure_ctx.verify_mode = ssl.CERT_NONE

        self._opener_verified = urllib.request.build_opener(
            *self._handlers_base, urllib.request.HTTPSHandler(context=self._verified_ctx)
        )
        self._opener_insecure = urllib.request.build_opener(
            *self._handlers_base, urllib.request.HTTPSHandler(context=self._insecure_ctx)
        )
        for opener in (self._opener_verified, self._opener_insecure):
            opener.addheaders = [
                ("User-Agent", user_agent),
                ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
                ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
                ("Accept-Encoding", "gzip, deflate"),
                ("Cache-Control", "no-cache"),
                ("Pragma", "no-cache"),
            ]
        # 兼容旧代码：insecure_tls=True 表示"全部不校验"
        self._global_insecure = bool(insecure_tls) and tls_verify is None

    # ---------- TLS 选择 ----------

    def verifies_certificate(self, url: str) -> bool:
        if self._global_insecure:
            return False
        if self.tls_verify is not None:
            try:
                return bool(self.tls_verify(url))
            except Exception:  # noqa: BLE001
                return True
        return True

    def _opener_for(self, url: str):
        return self._opener_verified if self.verifies_certificate(url) else self._opener_insecure

    # ---------- 底层 ----------

    def request(
        self,
        url: str,
        *,
        method: str | None = None,
        data: bytes | None = None,
        headers: dict | None = None,
        follow_redirects: bool = False,
        max_redirects: int = 5,
        timeout: float | None = None,
    ) -> Response:
        """发一个请求。

        网络层错误（DNS 失败、连接超时、拒绝连接、TLS 失败）会抛
        ``urllib.error.URLError`` / ``OSError``，由调用方决定是"没网"还是
        "认证页不可达"。HTTP 错误码不会抛异常，而是作为 Response 返回，
        便于识别 captive portal。
        """
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for key, value in headers.items():
                req.add_header(key, value)
        return self._open(req, follow_redirects, max_redirects, timeout)

    def _open(self, req, follow_redirects: bool, max_redirects: int, timeout) -> Response:
        started = time.time()
        try:
            opener = self._opener_for(req.full_url)
            resp = opener.open(req, timeout=timeout or self.timeout)
            try:
                raw = resp.read(self.max_bytes)
            except Exception:  # noqa: BLE001
                raw = b""
            return Response(
                getattr(resp, "status", 200),
                resp.headers,
                _maybe_decompress(raw, resp.headers),
                resp.geturl(),
                time.time() - started,
            )
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(self.max_bytes) or b""
            except Exception:  # noqa: BLE001
                raw = b""
            resp = Response(
                exc.code,
                exc.headers,
                _maybe_decompress(raw, exc.headers),
                getattr(exc, "url", None) or req.full_url,
                time.time() - started,
            )
            if (
                follow_redirects
                and resp.is_redirect
                and max_redirects > 0
            ):
                new_url = urllib.parse.urljoin(resp.url, resp.location)
                new_method = req.get_method()
                new_data = req.data
                if resp.status in (301, 302, 303):
                    new_method, new_data = "GET", None
                elif new_data and not _same_origin_strict(new_url, req.full_url):
                    # 307/308 会重发 body：只要端点变了就绝不把凭据再送一次
                    return resp
                return self.request(
                    new_url,
                    method=new_method,
                    data=new_data,
                    headers={k: v for k, v in req.header_items() if k.lower() not in ("host", "content-length")},
                    follow_redirects=True,
                    max_redirects=max_redirects - 1,
                    timeout=timeout,
                )
            return resp

    # ---------- 便捷 ----------

    def get(self, url: str, *, follow_redirects: bool = True, headers: dict | None = None,
            timeout: float | None = None) -> Response:
        return self.request(url, method="GET", headers=headers,
                            follow_redirects=follow_redirects, timeout=timeout)

    def post_form(
        self,
        url: str,
        fields: dict,
        *,
        encoding: str | None = None,
        follow_redirects: bool = True,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> Response:
        enc = encoding or "utf-8"
        payload = urllib.parse.urlencode(fields, encoding=enc, errors="replace").encode("ascii")
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": _origin(url),
            "Referer": url,
        }
        if headers:
            hdrs.update(headers)
        return self.request(url, method="POST", data=payload, headers=hdrs,
                            follow_redirects=follow_redirects, timeout=timeout)

    def get_query(self, url: str, fields: dict, *, encoding: str | None = None,
                  follow_redirects: bool = True, timeout: float | None = None) -> Response:
        query = urllib.parse.urlencode(fields, encoding=encoding or "utf-8", errors="replace")
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        return self.request(f"{url}{sep}{query}", method="GET",
                            follow_redirects=follow_redirects, timeout=timeout)


def classify_network_error(exc: BaseException) -> str:
    """把网络层异常归类成可读的状态码，供 :mod:`campusnet.checker` 使用。

    返回值：``dns`` / ``timeout`` / ``refused`` / ``tls`` / ``proxy`` / ``unreachable``
    """
    reason = getattr(exc, "reason", exc)
    text = str(reason).lower()
    if isinstance(reason, ssl.SSLError) or "ssl" in text or "certificate" in text:
        return "tls"
    if "getaddrinfo" in text or "name or service not known" in text or "nodename" in text:
        return "dns"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "refused" in text:
        return "refused"
    if "proxy" in text:
        return "proxy"
    if "unreachable" in text or "network is down" in text or "no route" in text:
        return "unreachable"
    return "unreachable"

"""极简 HTTP 客户端（纯标准库）。

针对校园网场景做的三件特殊处理：
1. 默认**关闭系统代理**：如果本机跑着 Clash / v2ray，探测流量会被代理接走，
   结果就是"明明没认证却显示已联网"，自动登录永远不触发。
2. 默认**不跟随重定向**：captive portal 的识别就是靠 302 的 Location，
   跟过去了就丢掉了唯一的线索。
3. 中文门户大量使用 GBK，所以这里做了 charset 嗅探。
"""

from __future__ import annotations

import gzip
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.cookiejar import CookieJar

from .config import DEFAULT_USER_AGENT

_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.I)
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class Response:
    __slots__ = ("status", "headers", "body", "url", "_text")

    def __init__(self, status: int, headers, body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url
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


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 8,
        insecure_tls: bool = True,
        use_system_proxy: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.timeout = timeout
        self.jar = CookieJar()
        handlers: list = [urllib.request.HTTPCookieProcessor(self.jar)]
        if not use_system_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        ctx = ssl.create_default_context()
        if insecure_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        handlers.append(_NoRedirect())
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [
            ("User-Agent", user_agent),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
            ("Accept-Encoding", "gzip, deflate"),
            ("Cache-Control", "no-cache"),
            ("Pragma", "no-cache"),
        ]
        self.user_agent = user_agent

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

        网络层错误（DNS 失败、连接超时、拒绝连接）会抛 urllib.error.URLError，
        由调用方决定是"没网"还是"认证页不可达"。HTTP 错误码不会抛异常，
        而是作为 Response 返回，便于识别 captive portal。
        """
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for key, value in headers.items():
                req.add_header(key, value)
        return self._open(req, follow_redirects, max_redirects, timeout)

    def _open(self, req, follow_redirects: bool, max_redirects: int, timeout) -> Response:
        try:
            resp = self.opener.open(req, timeout=timeout or self.timeout)
            raw = resp.read()
            return Response(
                getattr(resp, "status", 200),
                resp.headers,
                _maybe_decompress(raw, resp.headers),
                resp.geturl(),
            )
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read() or b""
            except Exception:  # noqa: BLE001
                raw = b""
            resp = Response(
                exc.code,
                exc.headers,
                _maybe_decompress(raw, exc.headers),
                getattr(exc, "url", None) or req.full_url,
            )
            if (
                follow_redirects
                and resp.status in _REDIRECT_CODES
                and resp.location
                and max_redirects > 0
            ):
                new_url = urllib.parse.urljoin(resp.url, resp.location)
                new_method = req.get_method()
                new_data = req.data
                if resp.status in (301, 302, 303):
                    new_method, new_data = "GET", None
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

    def get(self, url: str, *, follow_redirects: bool = True, headers: dict | None = None) -> Response:
        return self.request(url, method="GET", headers=headers, follow_redirects=follow_redirects)

    def post_form(
        self,
        url: str,
        fields: dict,
        *,
        encoding: str | None = None,
        follow_redirects: bool = True,
        headers: dict | None = None,
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
        return self.request(url, method="POST", data=payload, headers=hdrs, follow_redirects=follow_redirects)

    def get_query(self, url: str, fields: dict, *, encoding: str | None = None, follow_redirects: bool = True) -> Response:
        query = urllib.parse.urlencode(fields, encoding=encoding or "utf-8", errors="replace")
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        return self.request(f"{url}{sep}{query}", method="GET", follow_redirects=follow_redirects)


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


def is_cross_origin(a: str, b: str) -> bool:
    return _origin(a) != _origin(b)

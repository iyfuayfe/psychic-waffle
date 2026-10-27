"""HTTP 客户端测试。

重点验证 P0 的两条约束：

* POST 提交的账号密码**不会**被 307/308 重定向带到另一个端点；
* TLS 校验是"逐主机"决定的，默认校验、只有点名的主机才放行。
"""

from __future__ import annotations

import unittest

from tests.support import http_server

from campusnet.config import Config
from campusnet.http import HttpClient, decode_body


def _redirect_to(target_url: str, status: int = 307):
    def handler(_request):
        return status, {"Location": target_url}, b""

    return handler


class RedirectCredentialTests(unittest.TestCase):
    def test_cross_origin_307_does_not_resend_credentials(self):
        received: list = []

        def steal(request):
            received.append(request)
            return 200, {}, b"stolen"

        with http_server({("POST", "/steal"): steal}) as (other, _hits):
            with http_server({("POST", "/login"): _redirect_to(f"{other}/steal")}) as (origin, _o):
                client = HttpClient(timeout=5, insecure_tls=False)
                resp = client.post_form(
                    f"{origin}/login", {"username": "u", "password": "topsecret"},
                    follow_redirects=True,
                )
                self.assertEqual(resp.status, 307, "跨端点重定向时应该停在 3xx，不再发凭据")
                self.assertEqual(received, [], "另一个端点绝不能收到带密码的请求体")

    def test_same_origin_307_may_continue(self):
        """同一端点的 307（比如 /login -> /login2）允许继续，仍然带 body。"""
        seen: list = []

        def second(request):
            seen.append(getattr(request, "_body", b""))
            return 200, {}, b"ok"

        with http_server({
            ("POST", "/login"): _redirect_to("/login2", 307),
            ("POST", "/login2"): second,
        }) as (origin, _hits):
            client = HttpClient(timeout=5)
            resp = client.post_form(f"{origin}/login", {"password": "topsecret"},
                                    follow_redirects=True)
            self.assertEqual(resp.status, 200)
            self.assertTrue(seen and b"topsecret" in seen[0])

    def test_302_downgrades_to_get_without_body(self):
        seen: list = []

        def target(request):
            seen.append((request.command, request.headers.get("Content-Length")))
            return 200, {}, b"ok"

        with http_server({
            ("POST", "/login"): _redirect_to("/home", 302),
            ("GET", "/home"): target,
        }) as (origin, _hits):
            client = HttpClient(timeout=5)
            resp = client.post_form(f"{origin}/login", {"password": "x"}, follow_redirects=True)
            self.assertEqual(resp.status, 200)
            self.assertEqual(seen[0][0], "GET")
            self.assertIn(seen[0][1], (None, "0"))


class RedirectDefaultTests(unittest.TestCase):
    def test_redirects_are_not_followed_by_default(self):
        with http_server({("GET", "/a"): _redirect_to("/b", 302),
                          ("GET", "/b"): lambda request: (200, {}, b"b")}) as (origin, _hits):
            client = HttpClient(timeout=5)
            resp = client.get(f"{origin}/a", follow_redirects=False)
            self.assertEqual(resp.status, 302)
            self.assertTrue(resp.is_redirect)
            self.assertEqual(resp.location, "/b")


class TlsPolicyTests(unittest.TestCase):
    def test_default_verifies_certificate(self):
        self.assertTrue(HttpClient().verifies_certificate("https://example.com/"))

    def test_per_host_exception_from_config(self):
        cfg = Config(tls_insecure_hosts=["10.1.2.3"])
        client = HttpClient(tls_verify=cfg.tls_verify_for)
        self.assertFalse(client.verifies_certificate("https://10.1.2.3/portal"))
        self.assertTrue(client.verifies_certificate("https://www.example.com/"))

    def test_legacy_global_switch_still_supported(self):
        client = HttpClient(insecure_tls=True)
        self.assertFalse(client.verifies_certificate("https://example.com/"))


class MiscTests(unittest.TestCase):
    def test_gbk_page_is_decoded(self):
        text = decode_body("<html>上网登录页</html>".encode("gbk"),
                           "text/html; charset=gb2312")
        self.assertIn("上网登录页", text)

    def test_gzip_bomb_is_truncated(self):
        """只限制压缩前大小挡不住 gzip 炸弹：解压后必须二次限流。"""
        import gzip

        from campusnet import http as http_module

        bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))   # 压缩后很小，解压后 50 MB
        body = http_module._maybe_decompress(bomb, {"Content-Encoding": "gzip"}, 1024)
        self.assertLessEqual(len(body), 1024, "解压结果没有按 limit 截断")

    def test_deflate_is_truncated_too(self):
        import zlib

        from campusnet import http as http_module

        raw = zlib.compress(b"A" * (5 * 1024 * 1024))
        body = http_module._maybe_decompress(raw, {"Content-Encoding": "deflate"}, 512)
        self.assertLessEqual(len(body), 512)

    def test_response_is_capped(self):
        big = b"<html>" + b"x" * 5000 + b"</html>"
        with http_server({("GET", "/big"): lambda request: (200, {}, big)}) as (origin, _hits):
            client = HttpClient(timeout=5, max_bytes=1024)
            resp = client.get(f"{origin}/big")
            self.assertLessEqual(len(resp.body), 1024)

    def test_missing_page_returns_404_object_not_exception(self):
        with http_server({("GET", "/ok"): lambda request: (200, {}, b"ok")}) as (origin, _hits):
            client = HttpClient(timeout=5)
            resp = client.get(f"{origin}/missing")
            self.assertEqual(resp.status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

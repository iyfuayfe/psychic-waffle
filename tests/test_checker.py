"""联网探测状态分类测试。

任务书明确要求区分「Wi-Fi 没连」「DNS 失败」「超时」「被重定向到门户」
「内容劫持」等情况，不能统统报成"认证失败"。这里逐条验证。
"""

from __future__ import annotations

import socket
import ssl
import unittest
import urllib.error

from tests.support import FakeResponse

from campusnet.checker import (
    NETWORK_LAYER_KINDS,
    PORTAL_KINDS,
    STATE_DNS,
    STATE_HIJACK,
    STATE_HTTP,
    STATE_NO_TARGETS,
    STATE_ONLINE,
    STATE_REDIRECT,
    STATE_TIMEOUT,
    STATE_TLS,
    STATE_UNREACHABLE,
    check,
    describe_kind,
)
from campusnet.config import Config
from campusnet.http import classify_network_error


class StubClient:
    """按 URL 返回预设响应或抛预设异常。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def request(self, url, *, method=None, data=None, headers=None,
                follow_redirects=False, max_redirects=5, timeout=None):
        self.calls.append(url)
        result = self.mapping.get(url)
        if result is None:
            raise urllib.error.URLError("no route")
        if isinstance(result, BaseException):
            raise result
        return result


def config_with(*urls):
    cfg = Config()
    cfg.check_urls = [{"url": u} for u in urls]
    cfg.request_timeout = 3
    return cfg


ONLINE_BODY = "Microsoft Connect Test"
TARGET_A = "http://check.test/connecttest.txt"
TARGET_B = "http://check2.test/generate_204"


class OnlineTests(unittest.TestCase):
    def test_first_target_matches_means_online(self):
        cfg = config_with(TARGET_A, TARGET_B)
        cfg.check_urls[0]["expect_text"] = "Microsoft Connect Test"
        client = StubClient({TARGET_A: FakeResponse(200, ONLINE_BODY, TARGET_A)})
        result = check(client, cfg)
        self.assertTrue(result.online)
        self.assertEqual(result.kind, STATE_ONLINE)
        self.assertIn("预期内容", result.detail)
        self.assertEqual(len(client.calls), 1, "命中后不该继续探测其它目标")

    def test_expect_status_204(self):
        cfg = Config()
        cfg.check_urls = [{"url": TARGET_B, "expect_status": 204}]
        client = StubClient({TARGET_B: FakeResponse(204, "", TARGET_B)})
        self.assertTrue(check(client, cfg).online)


class PortalDetectionTests(unittest.TestCase):
    def test_redirect_gives_portal_url(self):
        cfg = config_with(TARGET_A)
        cfg.check_urls[0]["expect_text"] = "Microsoft Connect Test"
        location = "http://portal.test/a79.htm?wlanuserip=10.0.0.5"
        client = StubClient({
            TARGET_A: FakeResponse(302, "", TARGET_A, {"Location": location}),
        })
        result = check(client, cfg)
        self.assertFalse(result.online)
        self.assertEqual(result.kind, STATE_REDIRECT)
        self.assertEqual(result.portal_url, location)
        self.assertTrue(result.needs_portal_auth)
        self.assertFalse(result.is_network_layer)

    def test_hijack_returns_html_page(self):
        cfg = config_with(TARGET_A)
        cfg.check_urls[0]["expect_text"] = "Microsoft Connect Test"
        html = "<html><title>上网登录页</title><body>请登录</body></html>"
        client = StubClient({
            TARGET_A: FakeResponse(200, html, TARGET_A,
                                   {"Content-Type": "text/html; charset=gb2312"}),
        })
        result = check(client, cfg)
        self.assertEqual(result.kind, STATE_HIJACK)
        self.assertIn("上网登录页", result.page_html)


class NetworkLayerTests(unittest.TestCase):
    def test_dns_failure_is_not_auth_failure(self):
        cfg = config_with(TARGET_A)
        cfg.check_urls[0]["expect_text"] = "x"
        error = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        result = check(StubClient({TARGET_A: error}), cfg)
        self.assertEqual(result.kind, STATE_DNS)
        self.assertTrue(result.is_network_layer)
        self.assertFalse(result.needs_portal_auth)
        self.assertIn("域名解析失败", result.explain())

    def test_timeout(self):
        cfg = config_with(TARGET_A)
        error = urllib.error.URLError(socket.timeout("timed out"))
        result = check(StubClient({TARGET_A: error}), cfg)
        self.assertEqual(result.kind, STATE_TIMEOUT)

    def test_tls_error(self):
        cfg = config_with(TARGET_A)
        error = urllib.error.URLError(ssl.SSLError("certificate verify failed"))
        result = check(StubClient({TARGET_A: error}), cfg)
        self.assertEqual(result.kind, STATE_TLS)
        self.assertIn("证书", result.explain())

    def test_unknown_error_falls_back_to_unreachable(self):
        cfg = config_with(TARGET_A)
        result = check(StubClient({TARGET_A: OSError("boom")}), cfg)
        self.assertEqual(result.kind, STATE_UNREACHABLE)

    def test_no_targets(self):
        cfg = Config()
        cfg.check_urls = [{"url": ""}]
        result = check(StubClient({}), cfg)
        self.assertEqual(result.kind, STATE_NO_TARGETS)

    def test_error_codes_are_classified(self):
        self.assertEqual(classify_network_error(
            urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))), "dns")
        self.assertEqual(classify_network_error(
            urllib.error.URLError(socket.timeout("timed out"))), "timeout")
        self.assertEqual(classify_network_error(
            urllib.error.URLError(ssl.SSLError("certificate verify failed"))), "tls")
        self.assertEqual(classify_network_error(
            urllib.error.URLError(ConnectionRefusedError("refused"))), "refused")


class MultiTargetTests(unittest.TestCase):
    def test_one_dead_probe_site_does_not_mean_offline(self):
        """单个探测站点抽风时，不能判定校园网认证失败。"""
        cfg = Config()
        cfg.check_urls = [
            {"url": TARGET_A, "expect_text": "Microsoft Connect Test"},
            {"url": TARGET_B, "expect_status": 204},
        ]
        client = StubClient({
            TARGET_A: urllib.error.URLError(socket.timeout("timed out")),
            TARGET_B: FakeResponse(204, "", TARGET_B),
        })
        result = check(client, cfg)
        self.assertTrue(result.online, "第二个目标成功就应该判定在线")

    def test_all_fail_reports_most_informative_reason(self):
        cfg = Config()
        cfg.check_urls = [
            {"url": TARGET_A, "expect_text": "Microsoft Connect Test"},
            {"url": TARGET_B, "expect_status": 204},
        ]
        client = StubClient({
            TARGET_A: urllib.error.URLError(socket.timeout("timed out")),
            TARGET_B: urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
        })
        result = check(client, cfg)
        self.assertFalse(result.online)
        self.assertIn(result.kind, NETWORK_LAYER_KINDS)
        self.assertIn(result.kind, (STATE_TIMEOUT, STATE_DNS))

    def test_http_error_status(self):
        cfg = config_with(TARGET_A)
        cfg.check_urls[0]["expect_text"] = "x"
        client = StubClient({TARGET_A: FakeResponse(503, "busy", TARGET_A,
                                                    {"Content-Type": "text/plain"})})
        result = check(client, cfg)
        self.assertFalse(result.online)
        self.assertEqual(result.kind, STATE_HTTP)


class ContractTests(unittest.TestCase):
    def test_portal_kinds_and_network_kinds_are_disjoint(self):
        self.assertFalse(PORTAL_KINDS & NETWORK_LAYER_KINDS)

    def test_every_kind_has_a_readable_hint(self):
        for kind in PORTAL_KINDS | NETWORK_LAYER_KINDS | {STATE_ONLINE, STATE_HTTP}:
            text = describe_kind(kind)
            self.assertTrue(text and text != "网络状态未知。", kind)

    def test_backward_compatible_reason_attribute(self):
        cfg = config_with(TARGET_A)
        client = StubClient({TARGET_A: FakeResponse(302, "", TARGET_A,
                                                    {"Location": "http://p.test/"})})
        result = check(client, cfg)
        self.assertEqual(result.reason, result.kind)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

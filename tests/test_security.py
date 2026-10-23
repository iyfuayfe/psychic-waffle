"""安全基元测试：脱敏、URL 策略、认证页净化。

这些是 P0 修复的"证据测试"：它们直接断言"密码不会被写进日志"、
"账号密码不会被提交到未确认的主机"。
"""

from __future__ import annotations

import unittest

from tests.support import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from campusnet.security import (
    Decision,
    UrlPolicy,
    host_key,
    is_http_url,
    mask_account,
    origin_of,
    redact_portal_html,
    redact_text,
    redact_url,
    same_host,
    same_origin,
)


class RedactionTests(unittest.TestCase):
    def test_password_fields_are_masked(self):
        text = "password=hunter2&upass=abc123&user_password=pw&pwd=zzz"
        out = redact_text(text)
        for secret in ("hunter2", "abc123", "=pw", "zzz"):
            self.assertNotIn(secret, out)
        self.assertIn("password=", out)  # 字段名保留，方便看日志

    def test_account_is_masked_but_domain_kept(self):
        out = redact_text("uid='13800000000@cmcc' account=13800000000")
        self.assertNotIn("13800000000", out)
        self.assertIn("@cmcc", out)

    def test_long_ciphertext_is_truncated(self):
        token = "A" * 64
        self.assertNotIn(token, redact_text(f"params={token}"))

    def test_mobile_number_masked(self):
        out = redact_text("联系 13812345678")
        self.assertNotIn("13812345678", out)
        self.assertIn("138****5678", out)

    def test_normal_diagnostics_survive(self):
        out = redact_text("无法上网：DNS 解析失败 http://www.msftconnecttest.com/connecttest.txt")
        self.assertIn("DNS 解析失败", out)
        self.assertIn("msftconnecttest", out)

    def test_mask_account_short_and_long(self):
        self.assertEqual(mask_account("abc"), "ab***")
        self.assertEqual(mask_account("13800000000"), "138****0000")
        self.assertEqual(mask_account(""), "")


class UrlRedactionTests(unittest.TestCase):
    def test_query_values_are_hidden(self):
        url = "http://192.0.2.10/a79.htm?wlanuserip=198.51.100.20&mac=0a-1b-2c-3d-4e-5f"
        out = redact_url(url)
        self.assertNotIn("198.51.100.20", out)
        self.assertNotIn("08-9d-f4", out)
        self.assertIn("wlanuserip=", out)

    def test_path_and_host_are_kept_for_diagnosis(self):
        out = redact_url("https://auth.example.edu.cn/login?token=abc")
        self.assertIn("auth.example.edu.cn/login", out)

    def test_internal_ip_is_masked(self):
        self.assertNotIn("198.51.100.20", redact_url("http://198.51.100.20/a79.htm"))


class PortalDumpTests(unittest.TestCase):
    SAMPLE = """<html><script>
        v46ip='198.51.100.20';ss4="0a1b2c3d4e5f";
        uid='13800000000@cmcc';pwd='';
        authloginpath='/eportal/?c=ACSetting&a=Login';
        token='deadbeefdeadbeefdeadbeefdeadbeef';
    </script><body>上网登录页</body></html>"""

    def test_dump_has_no_credentials_or_addresses(self):
        out = redact_portal_html(self.SAMPLE, "http://192.0.2.10/a79.htm?wlanuserip=198.51.100.20")
        self.assertNotIn("13800000000", out)
        self.assertNotIn("198.51.100.20", out)
        self.assertNotIn("0a1b2c3d4e5f", out)
        self.assertNotIn("deadbeef", out)

    def test_dump_keeps_structure_for_troubleshooting(self):
        out = redact_portal_html(self.SAMPLE)
        self.assertIn("上网登录页", out)
        self.assertIn("authloginpath", out)


class HostAndOriginTests(unittest.TestCase):
    def test_host_key_normalises(self):
        self.assertEqual(host_key("http://Example.EDU.cn:80/x"), "example.edu.cn")
        self.assertEqual(host_key("https://a.test:8443/"), "a.test")
        self.assertEqual(host_key(""), "")

    def test_same_host_ignores_scheme_and_port(self):
        """同一台服务器的不同端口仍属同一方（门户 80 + 认证接口 801 很常见）。"""
        self.assertTrue(same_host("http://1.2.3.4/a", "https://1.2.3.4:80/b"))
        self.assertTrue(same_host("http://1.2.3.4/a", "http://1.2.3.4:801/login"))
        self.assertFalse(same_host("http://1.2.3.4/a", "http://1.2.3.5/a"))

    def test_same_origin_checks_scheme_and_port(self):
        self.assertTrue(same_origin("http://a.test/x", "http://a.test/y"))
        self.assertFalse(same_origin("http://a.test/x", "https://a.test/y"))
        self.assertFalse(same_origin("http://a.test:801/x", "http://a.test/y"))


class TlsHostKeyTests(unittest.TestCase):
    def test_tls_host_match_uses_hostname(self):
        from campusnet.config import Config

        cfg = Config(tls_insecure_hosts=["192.0.2.10"])
        self.assertFalse(cfg.tls_verify_for("https://192.0.2.10:8443/x"))
        self.assertTrue(cfg.tls_verify_for("https://111.17.200.131/x"))

    def test_origin_of_rejects_non_http(self):
        self.assertEqual(origin_of("file:///c:/x.html"), "")
        self.assertEqual(origin_of("javascript:alert(1)"), "")
        self.assertEqual(origin_of("http://a.test/x"), "http://a.test")

    def test_is_http_url(self):
        self.assertTrue(is_http_url("https://a.test"))
        self.assertFalse(is_http_url("ftp://a.test"))
        self.assertFalse(is_http_url(""))
        self.assertFalse(is_http_url("javascript:void(0)"))


class UrlPolicyTests(unittest.TestCase):
    def test_same_host_as_portal_allowed(self):
        policy = UrlPolicy()
        decision = policy.allow("http://192.0.2.10/eportal/login",
                                "http://192.0.2.10/a79.htm")
        self.assertTrue(decision)
        self.assertIsInstance(decision, Decision)

    def test_foreign_host_denied_with_actionable_reason(self):
        policy = UrlPolicy()
        decision = policy.allow("http://evil.example.com/steal", "http://192.0.2.10/a79.htm")
        self.assertFalse(decision)
        self.assertIn("未确认的地址", decision.reason)
        self.assertIn("trusted_auth_hosts", decision.reason)

    def test_trusted_hosts_entry_allows_separate_api_domain(self):
        policy = UrlPolicy(["https://auth.school.edu.cn"])
        decision = policy.allow("https://auth.school.edu.cn/api/login",
                                "http://10.0.0.1/portal")
        self.assertTrue(decision)

    def test_trusted_host_stored_as_url_is_normalised(self):
        policy = UrlPolicy(["auth.school.edu.cn:8443"])
        self.assertTrue(policy.allow("https://auth.school.edu.cn:8443/login", "http://10.0.0.1/"))

    def test_non_http_target_rejected(self):
        self.assertFalse(UrlPolicy().allow("file:///c:/passwords.txt", "http://a.test/"))

    def test_portal_policy_rejects_weird_scheme(self):
        self.assertFalse(UrlPolicy().allow_portal("javascript:alert(1)"))
        self.assertTrue(UrlPolicy().allow_portal("http://10.0.0.1/"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

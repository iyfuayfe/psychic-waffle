"""认证执行器测试（不联网）。

覆盖 P0 的关键断言：

* 账号密码**不会**被提交到未确认的主机；
* 账号密码错误时不再换策略乱试（避免锁号）；
* 验证码页面走人工流程，不尝试绕过；
* 服务端说成功但外网不通时，判为失败（不信页面文字）。

用 :class:`tests.support.FakeClient` 记录每一次请求，因此可以精确断言
"凭据有没有离开本机、去了哪里"。
"""

from __future__ import annotations

import unittest

from tests.support import FakeClient, FakeResponse, Silence

from campusnet.adapters import (
    STATUS_BAD_CREDENTIALS,
    STATUS_CAPTCHA_REQUIRED,
    STATUS_NETWORK_ERROR,
    STATUS_REFUSED_TARGET,
    STATUS_SERVER_ERROR,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    STATUS_VERIFY_FAILED,
    adapter_plan,
)
from campusnet.authenticator import PortalAuthenticator
from campusnet.config import Config

PORTAL = "http://192.0.2.10/a79.htm"

FORM_PAGE = """<html><head><title>上网登录页</title></head><body>
<form action="/eportal/login" method="post">
  <input type="text" name="username">
  <input type="password" name="password">
  <input type="submit" value="登录">
</form></body></html>"""

CAPTCHA_PAGE = """<html><head><title>上网登录页</title></head><body>
<form action="/eportal/login" method="post">
  <input type="text" name="username">
  <input type="password" name="password">
  <input type="text" name="checkcode" placeholder="验证码">
</form></body></html>"""

NO_FORM_PAGE = "<html><head><title>奇怪的门户</title></head><body><p>请下载客户端</p></body></html>"


def make_cfg(**overrides):
    cfg = Config()
    cfg.username = "student"
    cfg.portal_url = PORTAL
    cfg.drcom.enabled = False          # 本测试只关心通用表单路径
    cfg.request_timeout = 3
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def login(auth, *, verify_value=True):
    return auth.login(PORTAL, "student", "secret-pw", lambda: verify_value)


class HostGuardTests(unittest.TestCase):
    def test_form_action_on_foreign_host_is_refused(self):
        """表单 action 指向别的域名 -> 必须拒绝提交，且凭据一个字节都没发出去。"""
        page = FORM_PAGE.replace('action="/eportal/login"',
                                 'action="http://evil.example.com/collect"')
        client = FakeClient({
            PORTAL: FakeResponse(200, page, PORTAL, {"Content-Type": "text/html"}),
        })
        cfg = make_cfg()
        result = login(PortalAuthenticator(client, cfg, Silence()))

        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_REFUSED_TARGET)
        self.assertIn("未确认", result.reason)
        # 只应请求过认证页本身
        urls = [item["url"] for item in client.requests]
        self.assertEqual(urls, [PORTAL], f"不该向 {urls} 提交凭据")

    def test_foreign_host_allowed_when_explicitly_trusted(self):
        page = FORM_PAGE.replace('action="/eportal/login"',
                                 'action="http://auth.school.edu.cn/login"')
        client = FakeClient({
            PORTAL: FakeResponse(200, page, PORTAL, {"Content-Type": "text/html"}),
            "POST http://auth.school.edu.cn/login": FakeResponse(200, "ok"),
        })
        cfg = make_cfg(trusted_auth_hosts=["auth.school.edu.cn"])
        result = login(PortalAuthenticator(client, cfg, Silence()), verify_value=True)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.status, STATUS_SUCCESS)
        posted = [item for item in client.requests if item["method"] == "POST"]
        self.assertTrue(posted)
        self.assertIn("secret-pw", posted[0]["data"].values())

    def test_custom_endpoint_on_untrusted_host_is_refused(self):
        cfg = make_cfg()
        cfg.custom.enabled = True
        cfg.custom.url = "http://evil.example.com/api"
        client = FakeClient({
            PORTAL: FakeResponse(200, FORM_PAGE, PORTAL, {"Content-Type": "text/html"}),
        })
        result = login(PortalAuthenticator(client, cfg, Silence()))
        self.assertEqual(result.status, STATUS_REFUSED_TARGET)
        self.assertFalse([r for r in client.requests if "evil" in r["url"]])

    def test_invalid_portal_url_refused_before_any_request(self):
        cfg = make_cfg()
        client = FakeClient({})
        auth = PortalAuthenticator(client, cfg, Silence())
        result = auth.login("javascript:alert(1)", "u", "p", lambda: True)
        self.assertFalse(result.ok)
        self.assertEqual(client.requests, [])


class SuccessAndFailureTests(unittest.TestCase):
    def _form_client(self, response_text="ok", history=None):
        mapping = {
            PORTAL: FakeResponse(200, FORM_PAGE, PORTAL, {"Content-Type": "text/html"}),
        }
        return FakeClient(mapping)

    def test_success_requires_real_connectivity(self):
        client = self._form_client()
        # 表单提交后的响应（任意 URL 都回 200）
        client.responses["*"] = FakeResponse(200, "welcome")
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, STATUS_SUCCESS)
        self.assertTrue(result.strategy.startswith("form:"))

    def test_server_says_ok_but_no_internet_is_failure(self):
        client = self._form_client()
        client.responses["*"] = FakeResponse(200, "登录成功")
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=False)
        self.assertFalse(result.ok)
        self.assertIn(result.status, (STATUS_VERIFY_FAILED, STATUS_NETWORK_ERROR))

    def test_bad_credentials_stop_immediately(self):
        client = self._form_client()
        client.responses["*"] = FakeResponse(200, "密码错误，请重新输入")
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_BAD_CREDENTIALS)
        self.assertTrue(result.bad_credentials)
        self.assertFalse(result.retryable)
        # 账号密码错误时不应继续尝试第二个变体
        posts = [r for r in client.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_captcha_requires_human(self):
        client = FakeClient({
            PORTAL: FakeResponse(200, CAPTCHA_PAGE, PORTAL, {"Content-Type": "text/html"}),
        })
        result = login(PortalAuthenticator(client, make_cfg(), Silence()))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_CAPTCHA_REQUIRED)
        self.assertTrue(result.needs_user)
        self.assertFalse([r for r in client.requests if r["method"] == "POST"],
                         "有验证码时不能尝试提交（不许绕过）")

    def test_no_form_is_unsupported_with_guidance(self):
        client = FakeClient({
            PORTAL: FakeResponse(200, NO_FORM_PAGE, PORTAL, {"Content-Type": "text/html"}),
        })
        cfg = make_cfg()
        result = login(PortalAuthenticator(client, cfg, Silence()))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertTrue(result.needs_user)
        self.assertIn("找不到登录表单", result.reason)

    def test_status_classification_uses_integer_code_not_text(self):
        """钉住 P1-4：判定必须看整数状态码，不能解析文案。

        造一个"文案里出现 HTTP 2、但真实状态码是 500"的输入：老的
        `"HTTP 2" in last_reason` 实现会误判成 verify-failed，这里必须判 server-error。
        """
        from campusnet.authenticator import classify_submit_failure

        self.assertEqual(
            classify_submit_failure(network_error=False, last_status=500, has_reason=True),
            STATUS_SERVER_ERROR,
        )
        self.assertEqual(
            classify_submit_failure(network_error=False, last_status=204, has_reason=True),
            STATUS_VERIFY_FAILED,
        )
        self.assertEqual(
            classify_submit_failure(network_error=True, last_status=200, has_reason=True),
            STATUS_NETWORK_ERROR,
        )
        self.assertEqual(
            classify_submit_failure(network_error=False, last_status=None, has_reason=False),
            STATUS_SERVER_ERROR,
        )

    def test_2xx_without_internet_is_verify_failed(self):
        """服务端 2xx 收下了提交、只是外网没通 -> verify-failed。

        判据必须是**整数状态码**：以前用 `"HTTP 2" in 文案` 去猜，文案一改
        （例如变成 "HTTP 2xx"）分类就会静默出错。
        """
        client = self._form_client()
        client.responses["*"] = FakeResponse(200, "ok")
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_VERIFY_FAILED)

    def test_5xx_is_server_error(self):
        client = self._form_client()
        client.responses["*"] = FakeResponse(500, "boom")
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_SERVER_ERROR)

    def test_portal_unreachable_is_network_error_and_retryable(self):
        import urllib.error

        client = FakeClient({PORTAL: urllib.error.URLError("connection reset")})
        result = login(PortalAuthenticator(client, make_cfg(), Silence()))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_NETWORK_ERROR)
        self.assertTrue(result.retryable)

    def test_no_portal_url(self):
        client = FakeClient({})
        cfg = make_cfg()
        result = PortalAuthenticator(client, cfg, Silence()).login("", "u", "p", lambda: False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STATUS_NETWORK_ERROR)


class DrcomFailureKeywordTests(unittest.TestCase):
    """Dr.COM 失败文案的分类：通用失败不能被当成密码错误（会提前终止所有策略）。"""

    def test_generic_failure_text_is_not_bad_credentials(self):
        from campusnet.drcom import CREDENTIAL_HINTS, SOFT_FAILURE_HINTS

        generic = "认证失败：当前页面已超时"
        self.assertFalse(any(k in generic.lower() for k in CREDENTIAL_HINTS), generic)
        self.assertTrue(any(k in generic.lower() for k in SOFT_FAILURE_HINTS), generic)

    def test_real_credential_errors_still_stop_the_loop(self):
        from campusnet.drcom import CREDENTIAL_HINTS

        for text in ("密码错误", "Invalid Password", "账号不存在", "已被锁定"):
            self.assertTrue(
                any(k in text.lower() for k in CREDENTIAL_HINTS), f"{text} 应判为密码类错误"
            )

    def test_session_error_is_not_bad_credentials(self):
        from campusnet.drcom import CREDENTIAL_HINTS

        for text in ("当前页面已超时,请重新刷新页面", "invalid session"):
            self.assertFalse(any(k in text.lower() for k in CREDENTIAL_HINTS), text)


class AlreadyOnlineTests(unittest.TestCase):
    def test_logged_in_page_plus_connectivity_is_success(self):
        page = """<html><head><title>注销页</title></head><body>您已在线</body></html>"""
        client = FakeClient({
            PORTAL: FakeResponse(200, page, PORTAL, {"Content-Type": "text/html"}),
        })
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.strategy, "already")

    def test_logged_in_page_without_connectivity_keeps_trying(self):
        page = """<html><head><title>注销页</title></head><body>您已在线</body></html>"""
        client = FakeClient({
            PORTAL: FakeResponse(200, page, PORTAL, {"Content-Type": "text/html"}),
        })
        client.responses["*"] = FakeResponse(200, "no form here")
        result = login(PortalAuthenticator(client, make_cfg(), Silence()), verify_value=False)
        self.assertFalse(result.ok)


class AdapterPlanTests(unittest.TestCase):
    def test_plan_lists_three_adapters_with_reasons(self):
        cfg = make_cfg()
        plan = adapter_plan(cfg, portal_url=PORTAL)
        names = [item.name for item in plan]
        self.assertEqual(names, ["custom", "drcom", "form"])
        self.assertFalse(plan[0].applicable)  # 默认没启用自定义接口
        self.assertTrue(all(item.reason for item in plan if not item.applicable))

    def test_drcom_detected_by_url(self):
        cfg = Config()
        plan = adapter_plan(cfg, portal_url="http://1.2.3.4/eportal/portal/login")
        drcom = next(item for item in plan if item.name == "drcom")
        self.assertTrue(drcom.applicable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

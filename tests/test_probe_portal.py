"""「探测认证地址」三态分类与门户证据的回归测试（3.1.1 修复）。

## 背景（真实故障，用户反馈）

用户在另一台**已经完成校园网认证、能正常上网**的电脑上点击「探测认证地址」，程序提示：

    未探测到认证页面：当前网络可用，未发生认证页面跳转。请重新连接网络后重试，或手动填写地址。

即：把一个完全正常的网络状态报成了"探测失败"。根因是 ``probe_portal()`` 只有一个
布尔 ``ok``，"已在线（本来就没有门户）"和"探测失败（无法判断）"被压成同一个失败。

## 本文件覆盖任务书要求的 6 个场景

======================  =====================================================
场景                    测试类
======================  =====================================================
1 已认证且互联网正常       :class:`Scenario1OnlineTests`
2 未认证 + 302 跳转门户    :class:`Scenario2RedirectTests`
3 超时 / DNS 失败          :class:`Scenario3UnknownTests`
4 普通网站 / 一般错误页     :class:`Scenario4NotAPortalTests`
5 跳转到非预期域名         :class:`Scenario5UnexpectedRedirectTests`
6 新电脑首次运行           :class:`Scenario6FreshInstallTests`
======================  =====================================================

## 原则

* **不访问任何真实外网**：探测目标一律指向本机 ``127.0.0.1`` 的模拟 HTTP 服务
  （:func:`tests.support.http_server`），或使用假客户端注入网络异常；
* 不使用真实账号 / 密码；
* 测试数据全部落在隔离目录（``CAMPUSNET_DATA_DIR``，见 ``tests/support.py``），
  绝不污染用户真实的 ``%LOCALAPPDATA%\\CampusNet``。
"""

from __future__ import annotations

import socket
import unittest
import urllib.error
from unittest import mock

from tests.support import data_dir, free_port, http_server

from campusnet import status
from campusnet.config import Config
from campusnet.security import keeps_portal_query
from campusnet.ui import gui_service as svc

# Dr.COM / 哆点门户的最小可识别样本（真实门户带这个注释 + 这个标题）
DRCOM_HTML = """<!DOCTYPE html>
<html><head><title>上网登录页</title></head><body>
<!--Dr.COMWebLoginID_0.htm-->
<form name="login" method="post" action="/eportal/?c=ACSetting">
  <input type="text" name="DDDDD" value="">
  <input type="password" name="upass" value="">
</form>
</body></html>"""

# 普通网站 / 维护公告：没有任何门户特征
PLAIN_HTML = """<!DOCTYPE html>
<html><head><title>系统维护公告</title></head><body>
<p>系统维护中，请稍后访问。</p>
</body></html>"""

# 运营商网关参数（校园网跳转必备）
PORTAL_QUERY = "wlanuserip=10.0.0.5&wlanacname=AC-TEST-01&nasip=10.0.0.1"

CHECK_PATH = "/connecttest.txt"
PORTAL_PATH = "/a79.htm"
PLAIN_PATH = "/plain.html"
ERROR_PATH = "/busy.html"


def config_for(base_url: str, *, path: str = CHECK_PATH) -> Config:
    """把探测目标指向本机模拟服务（绝不出网）。"""
    cfg = Config()
    cfg.check_urls = [{"url": f"{base_url}{path}", "expect_text": "Microsoft Connect Test"}]
    cfg.request_timeout = 3
    cfg.portal_url = ""
    return cfg


def ok_text(_request):
    return 200, {"Content-Type": "text/plain; charset=utf-8"}, "Microsoft Connect Test"


class _ProbeCase(unittest.TestCase):
    """公共设施：隔离数据目录 + 用给定配置调用 probe_portal()。"""

    def setUp(self):  # noqa: D102
        self._ctx = data_dir()
        self.data_dir_path = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)

    def probe(self, cfg: Config, url: str = "") -> dict:
        with mock.patch.object(svc, "load_config", lambda: cfg):
            return svc.probe_portal(url)


# ---------------------------------------------------------------------------
# 场景 1：已认证且互联网正常
# ---------------------------------------------------------------------------


class Scenario1OnlineTests(_ProbeCase):
    def test_online_is_not_reported_as_probe_failure(self):
        """★用户实际反馈的场景：能上网时点探测，不能报"探测失败"。"""
        routes = {("GET", CHECK_PATH): ok_text}
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))

        self.assertEqual(result["state"], status.PROBE_ONLINE)
        self.assertFalse(result["ok"], "没发现门户时 ok 仍为 False（旧语义保持不变）")
        self.assertEqual(result["url"], "")
        self.assertEqual(result["portal_url"], "")

        state = status.from_probe(result)
        self.assertEqual(state.kind, status.KIND_ONLINE)
        self.assertEqual(state.severity, status.SEV_GOOD)
        self.assertEqual(state.label, "网络连接正常")

        text = state.describe()
        self.assertIn("网络连接正常", text)
        self.assertNotIn("探测失败", text)
        self.assertNotIn("未探测到认证页面", text)
        self.assertNotIn("重新连接网络", text)
        self.assertNotIn("重新连接网络", result.get("advice") or "")
        # 这正是用户看到的那句误导性建议
        self.assertNotIn("请重新连接网络后重试", result.get("detail", ""))

    def test_online_message_says_no_portal_needed(self):
        routes = {("GET", CHECK_PATH): ok_text}
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        state = status.from_probe(result)
        self.assertTrue(state.advice, "在线状态也要给一句可执行建议")
        self.assertIn("无需认证", state.advice + state.detail)

    def test_probe_hits_the_configured_target_only(self):
        routes = {("GET", CHECK_PATH): ok_text}
        with http_server(routes) as (base, hits):
            self.probe(config_for(base))
        self.assertTrue(hits, "应当真的发起了探测请求")
        self.assertEqual({method for method, _path, _headers in hits}, {"GET"})


# ---------------------------------------------------------------------------
# 场景 2：未认证，HTTP 被 302 到已知校园网门户
# ---------------------------------------------------------------------------


class Scenario2RedirectTests(_ProbeCase):
    def _routes(self):
        return {
            ("GET", CHECK_PATH): lambda _r: (
                302, {"Location": f"{PORTAL_PATH}?{PORTAL_QUERY}"}, "",
            ),
            ("GET", PORTAL_PATH): lambda _r: (
                200, {"Content-Type": "text/html; charset=utf-8"}, DRCOM_HTML,
            ),
        }

    def test_redirect_to_campus_portal_is_detected(self):
        with http_server(self._routes()) as (base, _hits):
            result = self.probe(config_for(base))

        self.assertTrue(result["ok"], "确实发现门户时 ok=True 语义不变")
        self.assertEqual(result["state"], status.PROBE_PORTAL)
        self.assertEqual(result["kind"], "redirect")
        self.assertTrue(result["from_redirect"])
        self.assertTrue(result["is_drcom"], "Dr.COM 门户应当被识别")
        self.assertTrue(result["persistable"], "有正向证据才允许写进配置")
        self.assertTrue(result["evidence"], "必须给出判断依据")
        self.assertEqual(result["title"], "上网登录页")

        state = status.from_probe(result)
        self.assertEqual(state.kind, status.KIND_NEEDS_AUTH)
        self.assertEqual(state.label, "发现认证门户")

    def test_portal_gateway_parameters_are_preserved(self):
        """跳转地址里的 wlanuserip / wlanacname / nasip 不能丢。"""
        with http_server(self._routes()) as (base, _hits):
            result = self.probe(config_for(base))
        url = result["url"]
        self.assertIn("wlanuserip=10.0.0.5", url)
        self.assertIn("wlanacname=AC-TEST-01", url)
        self.assertIn("nasip=10.0.0.1", url)

    def test_redirect_uses_no_post(self):
        with http_server(self._routes()) as (base, hits):
            self.probe(config_for(base))
        self.assertEqual({method for method, _p, _h in hits}, {"GET"})

    def test_saved_portal_address_is_verified_before_being_reported(self):
        """开发者机器的路径：配置里已有门户地址，探测只打开并校验它。"""
        routes = {
            ("GET", PORTAL_PATH): lambda _r: (
                200, {"Content-Type": "text/html; charset=utf-8"}, DRCOM_HTML,
            ),
        }
        with http_server(routes) as (base, _hits):
            cfg = config_for(base)
            cfg.portal_url = f"{base}{PORTAL_PATH}?{PORTAL_QUERY}"
            result = self.probe(cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], status.PROBE_PORTAL)
        self.assertTrue(result["persistable"])
        self.assertFalse(result["from_redirect"], "这次不是靠跳转发现的")


# ---------------------------------------------------------------------------
# 场景 3：未认证，但探测请求超时 / DNS 失败
# ---------------------------------------------------------------------------


class _StubClient:
    """按 URL 返回预设响应或抛预设异常（不产生任何真实网络流量）。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def _lookup(self, url):
        result = self.mapping.get(url)
        if result is None:
            raise urllib.error.URLError("no route")
        if isinstance(result, BaseException):
            raise result
        return result

    def request(self, url, *, method=None, data=None, headers=None,
                follow_redirects=False, max_redirects=5, timeout=None):
        self.calls.append((method or "GET", url))
        return self._lookup(url)

    def get(self, url, *, follow_redirects=True, headers=None, timeout=None):
        self.calls.append(("GET", url))
        return self._lookup(url)


TARGET = "http://check.test/connecttest.txt"


class Scenario3UnknownTests(_ProbeCase):
    def _probe_with_error(self, error: BaseException) -> tuple[dict, _StubClient]:
        cfg = Config()
        cfg.check_urls = [{"url": TARGET, "expect_text": "x"}]
        cfg.portal_url = ""
        client = _StubClient({TARGET: error})
        with mock.patch.object(svc, "load_config", lambda: cfg), \
             mock.patch.object(svc, "build_client", lambda _cfg: client):
            return svc.probe_portal(), client

    def test_dns_failure_is_unknown_and_not_a_portal(self):
        error = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        result, _client = self._probe_with_error(error)

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertEqual(result["kind"], "dns-error")
        self.assertEqual(result["url"], "")
        self.assertTrue(result["manual_entry"], "无法判断时必须给手工填写入口")

        state = status.from_probe(result)
        self.assertEqual(state.kind, status.KIND_NETWORK_UNAVAILABLE)
        self.assertEqual(state.label, "无法确定认证状态")
        self.assertNotIn("发现认证门户", state.describe())
        self.assertTrue(state.advice, "必须给出可执行建议")

    def test_timeout_is_unknown_and_not_a_portal(self):
        result, _client = self._probe_with_error(
            urllib.error.URLError(socket.timeout("timed out"))
        )
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertEqual(result["kind"], "timeout")
        self.assertEqual(result["url"], "")
        self.assertFalse(result["persistable"])

    def test_unknown_never_asks_user_to_reconnect_when_offline_reason_is_dns(self):
        """DNS 被拦时不能误导成"重新连接网络"，而要指出手动填写。"""
        error = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        result, _client = self._probe_with_error(error)
        advice = result.get("advice") or ""
        self.assertIn("手工填写", advice)

    def test_probe_does_not_submit_anything_on_failure(self):
        error = urllib.error.URLError(socket.timeout("timed out"))
        _result, client = self._probe_with_error(error)
        self.assertEqual({method for method, _url in client.calls}, {"GET"})


# ---------------------------------------------------------------------------
# 场景 4：返回普通网站 / 一般错误页面 —— 不得当成认证门户
# ---------------------------------------------------------------------------


class Scenario4NotAPortalTests(_ProbeCase):
    def test_generic_200_html_is_not_a_portal(self):
        routes = {("GET", CHECK_PATH): lambda _r: (
            200, {"Content-Type": "text/html; charset=utf-8"}, PLAIN_HTML,
        )}
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))

        self.assertFalse(result["ok"], "普通网页不能被当成认证门户")
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertEqual(result["url"], "")
        self.assertFalse(result["persistable"], "证据不足时绝不能写进配置")
        self.assertNotIn("发现认证门户", status.from_probe(result).describe())

    def test_redirect_to_plain_page_is_not_a_portal(self):
        routes = {
            ("GET", CHECK_PATH): lambda _r: (302, {"Location": PLAIN_PATH}, ""),
            ("GET", PLAIN_PATH): lambda _r: (
                200, {"Content-Type": "text/html; charset=utf-8"}, PLAIN_HTML,
            ),
        }
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertTrue(result["needs_confirm"])

    def test_server_error_status_is_not_a_portal(self):
        routes = {("GET", CHECK_PATH): lambda _r: (
            503, {"Content-Type": "text/html; charset=utf-8"},
            "<html><head><title>503</title></head><body>busy</body></html>",
        )}
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertIn(result["kind"], ("http-error", "unknown"))

    def test_error_page_behind_redirect_is_not_a_portal(self):
        routes = {
            ("GET", CHECK_PATH): lambda _r: (302, {"Location": ERROR_PATH}, ""),
            ("GET", ERROR_PATH): lambda _r: (500, {"Content-Type": "text/html"}, PLAIN_HTML),
        }
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertFalse(result["persistable"])

    def test_saved_address_that_is_not_a_portal_is_reported_honestly(self):
        """配置里存的地址其实是个普通网页时，不能报"已探测到认证页面"。"""
        routes = {("GET", PLAIN_PATH): lambda _r: (
            200, {"Content-Type": "text/html; charset=utf-8"}, PLAIN_HTML,
        )}
        with http_server(routes) as (base, _hits):
            cfg = config_for(base)
            cfg.portal_url = f"{base}{PLAIN_PATH}"
            result = self.probe(cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertFalse(result["persistable"])


# ---------------------------------------------------------------------------
# 场景 5：重定向到非预期域名
# ---------------------------------------------------------------------------


class Scenario5UnexpectedRedirectTests(_ProbeCase):
    def _routes(self, secret: str):
        return {
            ("GET", CHECK_PATH): lambda _r: (
                302,
                {"Location": f"{PLAIN_PATH}?uid=13812345678&ticket={secret}"},
                "",
            ),
            ("GET", PLAIN_PATH): lambda _r: (
                200, {"Content-Type": "text/html; charset=utf-8"}, PLAIN_HTML,
            ),
        }

    def test_unexpected_redirect_is_recorded_redacted_and_needs_confirmation(self):
        secret = "SECRET-TOKEN-9f8e7d6c"
        with http_server(self._routes(secret)) as (base, hits):
            result = self.probe(config_for(base))

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertTrue(result["needs_confirm"], "非预期页面必须人工确认")
        self.assertFalse(result["persistable"], "未经确认不得写进配置")

        candidate = result["candidate_url"]
        self.assertNotIn(secret, candidate, "诊断信息必须脱敏")
        self.assertNotIn("13812345678", candidate, "查询参数值必须打码")
        self.assertIn("127.0.0.1", candidate, "保留主机便于用户核对")
        self.assertIn("ticket=", candidate, "只保留参数名")

        self.assertEqual(
            {method for method, _p, _h in hits}, {"GET"},
            "探测阶段绝不能向该地址提交任何数据（更不能提交账号密码）",
        )

    def test_unreachable_redirect_target_is_unknown_not_portal(self):
        """跳转到一个打不开的地址：只能说"无法确定"，不能说"发现门户"。"""
        closed = f"http://127.0.0.1:{free_port()}/login"   # 本机未监听的端口
        routes = {("GET", CHECK_PATH): lambda _r: (302, {"Location": closed}, "")}
        with http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertEqual(result["url"], "")
        self.assertTrue(result["manual_entry"])


# ---------------------------------------------------------------------------
# 场景 6：新电脑首次运行（无历史配置、无缓存门户地址）
# ---------------------------------------------------------------------------


class Scenario6FreshInstallTests(_ProbeCase):
    def test_fresh_config_has_no_saved_portal(self):
        with data_dir():
            cfg = Config()
            self.assertEqual(cfg.portal_url, "", "新装默认没有门户地址，必须自动探测")

    def test_fresh_install_online(self):
        routes = {("GET", CHECK_PATH): ok_text}
        with data_dir(), http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        self.assertEqual(result["state"], status.PROBE_ONLINE)
        self.assertFalse(result["manual_entry"], "网络正常时不需要引导手工填写")

    def test_fresh_install_unconfigured_portal_redirect(self):
        routes = {
            ("GET", CHECK_PATH): lambda _r: (302, {"Location": f"{PORTAL_PATH}?{PORTAL_QUERY}"}, ""),
            ("GET", PORTAL_PATH): lambda _r: (
                200, {"Content-Type": "text/html; charset=utf-8"}, DRCOM_HTML,
            ),
        }
        with data_dir(), http_server(routes) as (base, _hits):
            result = self.probe(config_for(base))
        self.assertEqual(result["state"], status.PROBE_PORTAL)
        self.assertTrue(result["ok"])
        self.assertTrue(result["url"].startswith("http://127.0.0.1:"))

    def test_fresh_install_unknown_offers_manual_entry(self):
        cfg = Config()
        cfg.check_urls = [{"url": TARGET, "expect_text": "x"}]
        cfg.portal_url = ""
        client = _StubClient({TARGET: urllib.error.URLError(
            socket.gaierror(11001, "getaddrinfo failed"))})
        with data_dir(), mock.patch.object(svc, "load_config", lambda: cfg), \
             mock.patch.object(svc, "build_client", lambda _cfg: client):
            result = svc.probe_portal()
        self.assertEqual(result["state"], status.PROBE_UNKNOWN)
        self.assertTrue(result["manual_entry"])
        self.assertTrue(status.from_probe(result).advice)


# ---------------------------------------------------------------------------
# 门户地址写入的"参数退化"保护
# ---------------------------------------------------------------------------


class PortalPersistenceGuardTests(unittest.TestCase):
    def test_parameterized_address_is_not_overwritten_by_bare_one(self):
        old = "http://p.test/a79.htm?wlanuserip=1.2.3.4&nasip=5.6.7.8"
        self.assertFalse(keeps_portal_query(old, "http://p.test/a79.htm"))

    def test_upgrade_to_parameterized_address_is_allowed(self):
        self.assertTrue(keeps_portal_query("http://p.test/a79.htm",
                                           "http://p.test/a79.htm?wlanuserip=1.2.3.4"))

    def test_cross_host_update_is_allowed(self):
        self.assertTrue(keeps_portal_query("http://old.test/a79.htm?x=1",
                                           "http://new.test/a79.htm"))

    def test_empty_current_value_is_allowed(self):
        self.assertTrue(keeps_portal_query("", "http://p.test/a79.htm"))
        self.assertTrue(keeps_portal_query("http://p.test/a79.htm?x=1", ""))


class PersistWiringTests(unittest.TestCase):
    """``AuthPage._persist_portal`` 必须挡住参数退化（不需要真实 Tk 窗口）。"""

    class _App:
        def __init__(self, cfg):
            self.cfg = cfg
            self.saved = None

        def reload_cfg(self):
            return self.cfg

        def save_cfg(self, cfg):
            self.cfg = cfg
            self.saved = cfg.portal_url
            return True, "已保存"

    class _Stub:
        def __init__(self, cfg):
            self.app = PersistWiringTests._App(cfg)
            self.lines = []

        def _append(self, text, tag=None):
            self.lines.append(text)

    def _call(self, current: str, found: str):
        try:
            from campusnet.ui.page_auth import AuthPage
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"无法导入界面模块：{exc}")
        cfg = Config()
        cfg.portal_url = current
        stub = self._Stub(cfg)
        AuthPage._persist_portal(stub, found)
        return stub

    def test_losing_parameters_is_refused(self):
        rich = "http://p.test/a79.htm?wlanuserip=1.2.3.4&nasip=5.6.7.8"
        stub = self._call(rich, "http://p.test/a79.htm")
        self.assertIsNone(stub.app.saved, "参数更少的地址不得覆盖已保存的门户地址")
        self.assertTrue(any("保留原地址" in line for line in stub.lines), stub.lines)
        self.assertEqual(stub.app.cfg.portal_url, rich)

    def test_normal_update_is_saved(self):
        stub = self._call("", "http://p.test/a79.htm")
        self.assertEqual(stub.app.saved, "http://p.test/a79.htm")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
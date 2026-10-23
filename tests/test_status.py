"""统一状态机测试（campusnet/status.py）。

任务书要求界面必须能区分 11 种状态，且"状态必须与真实业务结果一致"。这里守住：

* 无线接口存在 ≠ 已联网（只有 online=True 才是连接成功）；
* 认证成功必须同时通过外网验证（AuthOutcome.ok 的语义）；
* 网络层错误 / 认证服务器错误 / 密码错误 三者不能混为一谈；
* 原因未知时不编造原因。
"""

from __future__ import annotations

import unittest

from tests.support import PROJECT_ROOT  # noqa: F401  (确保 sys.path 就绪)

from campusnet import status
from campusnet.config import Config


class KindCoverageTests(unittest.TestCase):
    def test_all_required_kinds_exist(self):
        """任务书点名的 11 个状态都要有对应 kind。"""
        required = [
            status.KIND_UNCONFIGURED,
            status.KIND_LOADING_CONFIG,
            status.KIND_CHECKING,
            status.KIND_AUTHENTICATING,
            status.KIND_VERIFYING,
            status.KIND_ONLINE,
            status.KIND_AUTH_FAILED,
            status.KIND_NETWORK_UNAVAILABLE,
            status.KIND_PORTAL_UNREACHABLE,
            status.KIND_CONFIG_ERROR,
            status.KIND_INTERNAL_ERROR,
        ]
        self.assertEqual(len(set(required)), len(required))

    def test_labels_are_human_readable(self):
        for state in (
            status.unconfigured(), status.loading_config(), status.config_error(),
            status.checking(), status.authenticating(), status.verifying(),
            status.online(), status.auth_failed(), status.network_unavailable(),
            status.portal_unreachable(), status.internal_error(),
        ):
            self.assertTrue(state.label.strip())
            self.assertIn(state.severity, (
                status.SEV_GOOD, status.SEV_WARN, status.SEV_BAD, status.SEV_MUTED))

    def test_failures_carry_actionable_advice(self):
        for state in (status.unconfigured(), status.config_error(),
                      status.auth_failed(), status.network_unavailable(),
                      status.portal_unreachable(), status.internal_error()):
            self.assertTrue(state.advice.strip(), f"{state.kind} 缺少可执行建议")


class FromCheckTests(unittest.TestCase):
    def test_interface_present_is_not_online(self):
        """'有无线网卡/已连 Wi-Fi' 不能被当作联网成功。"""
        state = status.from_check({"online": False, "kind": "unreachable", "detail": ""})
        self.assertNotEqual(state.kind, status.KIND_ONLINE)
        self.assertEqual(state.kind, status.KIND_NETWORK_UNAVAILABLE)

    def test_online_maps_to_success(self):
        state = status.from_check({"online": True, "kind": "online", "detail": "ok"})
        self.assertEqual(state.kind, status.KIND_ONLINE)
        self.assertTrue(state.ok)
        self.assertEqual(state.severity, status.SEV_GOOD)

    def test_portal_kinds_map_to_needs_auth(self):
        for kind in ("redirect", "hijack"):
            state = status.from_check({"online": False, "kind": kind,
                                       "portal_url": "http://10.0.0.1/"})
            self.assertEqual(state.kind, status.KIND_NEEDS_AUTH, kind)
            self.assertIn("认证", state.label)

    def test_network_layer_errors_are_distinguished(self):
        cases = {
            "dns-error": "解析",
            "timeout": "超时",
            "refused": "拒绝",
            "tls-error": "证书",
            "proxy-error": "代理",
            "unreachable": "不可达",
        }
        for kind, keyword in cases.items():
            state = status.from_check({"online": False, "kind": kind})
            self.assertEqual(state.kind, status.KIND_NETWORK_UNAVAILABLE, kind)
            self.assertIn(keyword, state.detail + state.advice, kind)

    def test_unknown_reason_is_not_invented(self):
        state = status.from_check({"online": False, "kind": "some-new-kind"})
        self.assertEqual(state.kind, status.KIND_NETWORK_UNAVAILABLE)
        self.assertIn("无法确定", state.detail)

    def test_accepts_object_with_attributes(self):
        class FakeResult:
            online = False
            kind = "timeout"
            detail = "请求超时"

        state = status.from_check(FakeResult())
        self.assertEqual(state.kind, status.KIND_NETWORK_UNAVAILABLE)
        self.assertIn("超时", state.detail)


class FromAuthTests(unittest.TestCase):
    def test_bad_credentials_is_not_network_error(self):
        state = status.from_auth({"ok": False, "status": "bad-credentials",
                                  "reason": "账号或密码不正确"})
        self.assertEqual(state.kind, status.KIND_AUTH_FAILED)
        self.assertIn("密码", state.advice)

    def test_server_error_maps_to_portal_unreachable(self):
        state = status.from_auth({"ok": False, "status": "server-error", "reason": "500"})
        self.assertEqual(state.kind, status.KIND_PORTAL_UNREACHABLE)
        self.assertIn("认证服务器", state.label)

    def test_network_error_maps_to_network_unavailable(self):
        state = status.from_auth({"ok": False, "status": "network-error", "reason": "超时"})
        self.assertEqual(state.kind, status.KIND_NETWORK_UNAVAILABLE)

    def test_captcha_and_manual_need_user(self):
        for status_name in ("captcha-required", "manual-required"):
            state = status.from_auth({"ok": False, "status": status_name, "reason": "x"})
            self.assertEqual(state.kind, status.KIND_NEEDS_USER, status_name)
            self.assertIn("浏览器", state.advice)

    def test_verify_failed_is_failure_not_success(self):
        state = status.from_auth({"ok": False, "status": "verify-failed",
                                  "reason": "服务端说成功但外网不通"})
        self.assertEqual(state.kind, status.KIND_AUTH_FAILED)
        self.assertFalse(state.ok)

    def test_success_maps_to_online(self):
        for status_name in ("success", "already-online"):
            state = status.from_auth({"ok": True, "status": status_name, "reason": "ok"})
            self.assertEqual(state.kind, status.KIND_ONLINE, status_name)
            self.assertEqual(state.severity, status.SEV_GOOD)

    def test_unknown_failure_status_is_honest(self):
        state = status.from_auth({"ok": False, "status": "brand-new", "reason": ""})
        self.assertEqual(state.kind, status.KIND_AUTH_FAILED)
        self.assertIn("无法确定", state.detail)


class FromHeartbeatTests(unittest.TestCase):
    def test_empty_heartbeat_returns_none(self):
        self.assertIsNone(status.from_heartbeat({}))
        self.assertIsNone(status.from_heartbeat(None))

    def test_heartbeat_states_map(self):
        cases = {
            "online": status.KIND_ONLINE,
            "checking": status.KIND_CHECKING,
            "auth": status.KIND_AUTHENTICATING,
            "wifi": status.KIND_CHECKING,
            "need-user": status.KIND_NEEDS_USER,
            "cooldown": status.KIND_NETWORK_UNAVAILABLE,
        }
        for raw, expected in cases.items():
            state = status.from_heartbeat({"state": raw, "detail": ""})
            self.assertIsNotNone(state)
            self.assertEqual(state.kind, expected, raw)

    def test_need_user_heartbeat_has_advice(self):
        state = status.from_heartbeat({"state": "need-user", "detail": "密码不对"})
        self.assertIn("浏览器认证", state.advice)


class ConfigStateTests(unittest.TestCase):
    def test_unconfigured_detected_from_real_config(self):
        """真实 Config 对象：没填账号/密码时应判为未配置。"""
        cfg = Config()
        self.assertFalse(cfg.is_configured())
        state = status.unconfigured()
        self.assertEqual(state.kind, status.KIND_UNCONFIGURED)
        self.assertEqual(state.severity, status.SEV_WARN)

    def test_describe_never_empty(self):
        for state in (status.unconfigured(), status.online(), status.internal_error("x")):
            self.assertTrue(state.describe().strip())

    def test_as_dict_has_ui_fields(self):
        payload = status.portal_unreachable("超时").as_dict()
        for key in ("kind", "label", "severity", "detail", "advice"):
            self.assertIn(key, payload)

    def test_busy_flags(self):
        self.assertTrue(status.checking().busy)
        self.assertTrue(status.authenticating().busy)
        self.assertFalse(status.online().busy)
        self.assertFalse(status.auth_failed().busy)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
